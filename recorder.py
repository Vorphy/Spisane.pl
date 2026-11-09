"""
recorder.py – nagrywanie dźwięku z głośników (WASAPI loopback) + mikrofonu,
miksowanie do mono i zapis na bieżąco do pliku Opus (PyAV, bez ffmpeg).
Tryb stereo=True: lewy kanał = mikrofon, prawy = rozmówcy (bez miksu) – do obróbki każdego osobno
po nagraniu (converter.convert_to_opus(split_channels=True) miksuje potem do mono).

Mikrofon jest "zegarem": co jego blok bierzemy tyle samo próbek z loopbacku.
Loopback w WASAPI nie dostarcza danych, gdy nic nie gra – wtedy dopełniamy ciszą.
"""

import queue
import threading
import time
from pathlib import Path

import numpy as np
import pyaudiowpatch as pa
import av

RATE = 48000          # Opus pracuje natywnie na 48 kHz
BLOCK = 960           # 20 ms – rozmiar ramki Opus
MAX_LAG = RATE // 2   # >0,5 s zaległości loopbacku = przycinamy (dryf zegarów)


class AGC:
    """
    Automatyczne wyrównywanie głośności jednego źródła (przetwarza bloki po 20 ms).

    - śledzi poziom mowy (szybko w górę, wolno w dół) i dąży do TARGET dB,
    - podgłaśnia powoli (≈5 dB/s), ścisza szybko – bez pompowania i przesterowań,
    - nie podgłaśnia szumu: próg aktywności = szum tła + 10 dB (szum tła śledzony na bieżąco),
    - w ciszy trzyma ostatnie wzmocnienie (rozmówca wraca od razu na dobrym poziomie).
    """
    TARGET = -20.0      # dBFS – poziom, przy którym Whisper dobrze słyszy mowę
    MAX_GAIN = 20.0     # dB – maks. podgłośnienie (×10)
    MIN_GAIN = -10.0    # dB – maks. ściszenie
    UP = 0.1            # dB na blok (20 ms) → 5 dB/s
    DOWN = 1.5          # dB na blok

    def __init__(self):
        self.gain = 0.0
        self.env = None
        self.floor = -60.0

    def process(self, x):
        out = np.empty_like(x)
        for i in range(0, len(x), BLOCK):
            blk = x[i:i + BLOCK]
            rms = float(np.sqrt(np.mean(blk * blk))) if len(blk) else 0.0
            db = 20 * np.log10(rms) if rms > 1e-7 else -140.0
            # szum tła: natychmiast w dół, bardzo powoli w górę
            if db < self.floor:
                self.floor = max(db, -90.0)
            else:
                self.floor += 0.01
            g0 = self.gain
            if db > max(-55.0, self.floor + 10):          # jest sygnał (mowa)
                if self.env is None:
                    self.env = db
                k = 0.2 if db > self.env else 0.02
                self.env += (db - self.env) * k
                want = min(self.MAX_GAIN, max(self.MIN_GAIN, self.TARGET - self.env))
                if want > self.gain:
                    self.gain = min(want, self.gain + self.UP)
                else:
                    self.gain = max(want, self.gain - self.DOWN)
            # płynne przejście wzmocnienia w obrębie bloku (bez trzasków)
            ramp = 10 ** (np.linspace(g0, self.gain, len(blk), dtype=np.float32) / 20)
            out[i:i + BLOCK] = blk * ramp
        return out


def list_devices():
    """Zwraca (wejścia, loopbacki, domyślne_wejście_idx, domyślny_loopback_idx)."""
    p = pa.PyAudio()
    try:
        w = p.get_host_api_info_by_type(pa.paWASAPI)
        inputs, loops = [], []
        for d in p.get_device_info_generator_by_host_api(host_api_index=w["index"]):
            item = (d["index"], d["name"])
            if d.get("isLoopbackDevice"):
                loops.append((d["index"], d["name"].replace(" [Loopback]", "")))
            elif d["maxInputChannels"] > 0:
                inputs.append(item)
        def_in = w["defaultInputDevice"]
        try:
            def_loop = p.get_default_wasapi_loopback()["index"]
        except Exception:
            def_loop = loops[0][0] if loops else None
        return inputs, loops, def_in, def_loop
    finally:
        p.terminate()


class _Source:
    """Jeden strumień wejściowy -> kolejka bloków mono float32 @ RATE."""

    def __init__(self, p, index, store=True):
        self.store = store          # False = tylko pomiar poziomu (podgląd bez nagrywania)
        self.last = 0.0
        info = p.get_device_info_by_index(index)
        self.channels = max(1, min(2, info["maxInputChannels"]))
        self.rate = int(info["defaultSampleRate"])
        self.q = queue.Queue()
        self.level = 0.0
        self.stream = p.open(format=pa.paFloat32, channels=self.channels, rate=self.rate,
                             input=True, input_device_index=index,
                             frames_per_buffer=int(self.rate * 0.02),
                             stream_callback=self._cb)

    def _cb(self, data, frames, time_info, status):
        a = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels).mean(axis=1)
        if self.rate != RATE:  # prosta interpolacja – wystarczy do mowy
            n = int(round(len(a) * RATE / self.rate))
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
        self.level = float(np.sqrt(np.mean(a * a))) if len(a) else 0.0
        self.last = time.time()
        if self.store:
            self.q.put(a)
        return (None, pa.paContinue)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


def _meter(s):
    """Poziom 0..1 w skali dB (-55 dB cisza .. -5 dB głośno). Brak danych >0,3 s = cisza
    (loopback WASAPI nie wysyła nic, gdy w głośnikach jest cisza)."""
    if not s or s.level <= 0 or time.time() - s.last > 0.3:
        return 0.0
    return max(0.0, min(1.0, (20 * np.log10(s.level) + 55) / 50))


class LevelMonitor:
    """Podgląd poziomów bez nagrywania: m = LevelMonitor(mic_idx, loop_idx); m.levels; m.close()"""

    def __init__(self, mic_index, loop_index):
        self._p = pa.PyAudio()
        self.mic = self.loop = None
        self.errors = []
        for attr, idx in (("mic", mic_index), ("loop", loop_index)):
            if idx is None:
                continue
            try:
                setattr(self, attr, _Source(self._p, idx, store=False))
            except Exception as e:
                self.errors.append(f"{attr}: {e}")

    @property
    def levels(self):
        return _meter(self.mic), _meter(self.loop)

    def close(self):
        for s in (self.mic, self.loop):
            if s:
                s.close()
        try:
            self._p.terminate()
        except Exception:
            pass


class Recorder:
    """Użycie: r = Recorder(path, mic_idx, loop_idx); r.start(); ... r.stop()"""

    def __init__(self, path, mic_index, loop_index, bitrate=32000, agc=True, stereo=False):
        self.path = Path(path)
        self.agc = agc
        self.stereo = stereo
        self.mic_index = mic_index
        self.loop_index = loop_index
        self.bitrate = bitrate
        self.started_at = None
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self.mic = self.loop = None

    # poziomy 0..1 do wskaźników w GUI
    @property
    def levels(self):
        return _meter(self.mic), _meter(self.loop)

    @property
    def elapsed(self):
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def size(self):
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def start(self):
        self._p = pa.PyAudio()
        try:
            self.mic = _Source(self._p, self.mic_index) if self.mic_index is not None else None
            self.loop = _Source(self._p, self.loop_index) if self.loop_index is not None else None
        except Exception:
            self._cleanup_streams()
            raise
        if not self.mic and not self.loop:
            raise RuntimeError("Nie wybrano żadnego źródła dźwięku.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._cleanup_streams()

    def _cleanup_streams(self):
        for s in (self.mic, self.loop):
            if s:
                s.close()
        try:
            self._p.terminate()
        except Exception:
            pass

    def _run(self):
        try:
            out = av.open(str(self.path), "w", format="ogg",
                          options={"flush_packets": "1"})  # zapis na bieżąco
            st = out.add_stream("libopus", rate=RATE, layout="stereo" if self.stereo else "mono")
            st.bit_rate = self.bitrate * (2 if self.stereo else 1)   # plik pośredni – bez straty na kanał
            mic_buf = np.zeros(0, np.float32)
            loop_buf = np.zeros(0, np.float32)
            pts = 0
            clock = self.mic or self.loop     # źródło wyznaczające tempo
            other = self.loop if clock is self.mic else None
            agc_a = AGC() if self.agc else None
            agc_b = AGC() if self.agc else None

            def drain(src, buf):
                while True:
                    try:
                        buf = np.concatenate([buf, src.q.get_nowait()])
                    except queue.Empty:
                        return buf

            while not self._stop.is_set() or not clock.q.empty():
                try:
                    chunk = clock.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                mic_buf = np.concatenate([mic_buf, chunk])
                if other:
                    loop_buf = drain(other, loop_buf)
                    if len(loop_buf) > MAX_LAG + len(mic_buf):
                        loop_buf = loop_buf[-(len(mic_buf) + BLOCK):]
                n = (len(mic_buf) // BLOCK) * BLOCK
                if n == 0:
                    continue
                a = mic_buf[:n]
                mic_buf = mic_buf[n:]
                if agc_a:
                    a = agc_a.process(a)
                if other:
                    b = loop_buf[:n]
                    loop_buf = loop_buf[len(b):]
                    if len(b) < n:
                        b = np.pad(b, (0, n - len(b)))
                    if agc_b:
                        b = agc_b.process(b)
                else:
                    b = None
                if self.stereo:
                    mic, loop = (a, b) if clock is self.mic else (None, a)
                    z = np.zeros(n, np.float32)
                    pcm = np.stack([np.tanh(mic if mic is not None else z),
                                    np.tanh(loop if loop is not None else z)], axis=1)   # przeplot L/R
                    frame = av.AudioFrame.from_ndarray(
                        (pcm * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="stereo")
                else:
                    if b is not None:
                        a = a + b
                    a = np.tanh(a)  # miękki limiter – suma dwóch źródeł nie przesteruje
                    frame = av.AudioFrame.from_ndarray(
                        (a * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = RATE
                frame.pts = pts
                pts += n
                for packet in st.encode(frame):
                    out.mux(packet)
            for packet in st.encode(None):
                out.mux(packet)
            out.close()
        except Exception as e:
            self.error = e
