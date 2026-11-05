"""
converter.py – konwersja dowolnego pliku audio/wideo do Opus mono (PyAV, bez ffmpeg),
opcjonalnie z WYRÓWNANIEM GŁOŚNOŚCI i REDUKCJĄ SZUMU (dwa przebiegi, z podglądem do przodu).

Wyrównanie: plik dzielony na ramki 20 ms; dla każdej ramki liczymy poziom mowy w oknie ±0,4 s
(tylko ramki ponad szumem tła) i wzmocnienie dociągające go do TARGET. Długie przerwy (bez mowy
w oknie) zostają nietknięte – szum nie jest podgłaśniany. Pamięć: tylko krzywa wzmocnienia
(~180 tys. liczb na godzinę), nie całe audio.
"""

from pathlib import Path
import numpy as np
import av

RATE = 48000
FRAME = 960                 # 20 ms

TARGET = -20.0              # dBFS – docelowy poziom mowy
MAX_GAIN = 20.0             # dB (×10)
MAX_GAIN_NR = 26.0          # dB (×20) – przy odszumianiu można mocniej, szum nie urośnie
MIN_GAIN = -10.0            # dB
WINDOW = 40                 # ramek = 0,8 s – okno „słowa/frazy”
SMOOTH = 10                 # ramek = 0,2 s – wygładzenie zmian wzmocnienia
FLOOR_SPAN = 750            # ramek = 15 s – okno do śledzenia szumu tła


def probe_duration(path):
    """Długość audio w sekundach (z nagłówka; dla Ogg liczona z ostatniego pakietu)."""
    with av.open(str(path)) as c:
        if c.duration:
            return c.duration / 1_000_000
        s = c.streams.audio[0]
        last = 0
        for pkt in c.demux(s):
            if pkt.pts is not None:
                last = max(last, float((pkt.pts + (pkt.duration or 0)) * s.time_base))
        return last


def _decode_mono(path, cancel=None, progress=None):
    """Generator bloków float32 mono 48 kHz."""
    with av.open(str(path)) as inp:
        if not inp.streams.audio:
            raise ValueError("Plik nie zawiera ścieżki dźwiękowej")
        ain = inp.streams.audio[0]
        total = (inp.duration / 1_000_000) if inp.duration else None
        rs = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        last = -1.0
        for frame in inp.decode(ain):
            if cancel and cancel():
                raise InterruptedError("Przerwano")
            for rf in rs.resample(frame):
                yield rf.to_ndarray().reshape(-1).astype(np.float32) / 32768
            if progress and total and frame.time is not None:
                f = min(1.0, frame.time / total)
                if f - last >= 0.01:
                    progress(f)
                    last = f
        for rf in rs.resample(None):
            yield rf.to_ndarray().reshape(-1).astype(np.float32) / 32768


def _activity(db):
    """Które ramki 20 ms to mowa (True), a które cisza/szum (False)."""
    n = len(db)
    # szum tła: 5. percentyl w ruchomym oknie 15 s (liczony blokami po 5 s).
    # Szum jest jednostajny (wąski rozrzut), mowa „skacze” – próg +6 dB odróżnia nawet cichą mowę.
    floor = np.empty(n)
    step = FLOOR_SPAN // 3
    for i in range(0, n, step):
        lo, hi = max(0, i - FLOOR_SPAN // 2), min(n, i + step + FLOOR_SPAN // 2)
        floor[i:i + step] = np.percentile(db[lo:hi], 5)
    return db > np.maximum(floor + 6, -65), floor


def _gain_curve(db, active, floor=None, top=MAX_GAIN):
    """dB na ramkę -> wzmocnienie dB na ramkę (top = maks. podgłośnienie)."""
    if len(db) == 0:
        return db
    energy = np.where(active, 10 ** (db / 10), 0.0)
    k = np.ones(WINDOW)
    e_sum = np.convolve(energy, k, mode="same")
    a_cnt = np.convolve(active.astype(float), k, mode="same")
    valid = a_cnt >= 5                                   # ≥100 ms mowy w oknie
    level = 10 * np.log10(np.maximum(e_sum, 1e-20) / np.maximum(a_cnt, 1))
    gain = np.where(valid, np.clip(TARGET - level, MIN_GAIN, top), 0.0)
    # wygładzenie, żeby nie było „schodków”
    gain = np.convolve(gain, np.ones(SMOOTH) / SMOOTH, mode="same")
    return gain


# ── Redukcja szumu (bramka widmowa) ────────────────────────────────────────
# STFT 960 próbek (20 ms) z przesunięciem 480, okno sqrt-Hann (idealna rekonstrukcja).
# Profil szumu: z ramek uznanych za ciszę (przebieg 1), w ruchomym oknie 15 s, w ~25 pasmach.
N_FFT = FRAME
HOP = FRAME // 2
_WIN = np.sqrt(np.hanning(N_FFT + 1)[:-1]).astype(np.float32)
_WPOW = float(np.sum(_WIN ** 2))
_BAND_EDGES = np.unique(np.round(np.geomspace(1, N_FFT // 2 + 1, 34)).astype(int))
_BIN_BAND = np.clip(np.searchsorted(_BAND_EDGES, np.arange(N_FFT // 2 + 1), side="right") - 1,
                    0, len(_BAND_EDGES) - 2)
_FREQS = np.fft.rfftfreq(N_FFT, 1 / RATE)
NR_FLOOR = 10 ** (-15 / 20)          # maks. tłumienie szumu: -15 dB (bez metalicznych artefaktów)
NR_OVER = 2.0                        # nadodejmowanie szumu
HP = np.clip((_FREQS - 60) / 60, 0.03, 1.0)   # filtr górnoprzepustowy ~60–120 Hz


def _band_psd(frames):
    """frames (k, N_FFT) -> średnia moc w pasmach (k, n_bands)."""
    X = np.fft.rfft(frames * _WIN, axis=1)
    p = (np.abs(X) ** 2) / _WPOW
    n_b = len(_BAND_EDGES) - 1
    out = np.zeros((len(frames), n_b), np.float32)
    np.add.at(out.T, _BIN_BAND, p.T)
    counts = np.bincount(_BIN_BAND, minlength=n_b).astype(np.float32)
    return out / np.maximum(counts, 1)


def _noise_profile(bands, active):
    """Profil szumu (pasma) na każdy blok 5 s – mediana ramek ciszy w oknie ±7,5 s."""
    n = len(bands)
    step = FLOOR_SPAN // 3
    quiet_all = bands[~active]
    glob = np.median(quiet_all, axis=0) if len(quiet_all) else np.full(bands.shape[1], 1e-12)
    prof = []
    for i in range(0, max(n, 1), step):
        lo, hi = max(0, i - FLOOR_SPAN // 2), min(n, i + step + FLOOR_SPAN // 2)
        q = bands[lo:hi][~active[lo:hi]]
        prof.append(np.median(q, axis=0) if len(q) >= 25 else glob)
    return np.array(prof, np.float32), step


class _Denoiser:
    """Strumieniowa bramka widmowa z nakładaniem (opóźnienie = N_FFT - HOP próbek, kompensowane)."""

    def __init__(self, profile, step):
        self.profile, self.step = profile, step
        self.inbuf = np.zeros(N_FFT - HOP, np.float32)   # „rozbieg” – kompensuje opóźnienie
        self.ola = np.zeros(N_FFT, np.float32)
        self.frame_pos = -(N_FFT - HOP)                  # pozycja początku bieżącej ramki
        self.prev_g = np.ones(N_FFT // 2 + 1, np.float32)
        self.skip = N_FFT - HOP                          # pierwsze próbki wyjścia to rozbieg
        self.k3 = np.ones(5, np.float32) / 5

    def _gain(self, X, center):
        blk = min(len(self.profile) - 1, max(0, int(center // FRAME) // self.step))
        noise = self.profile[blk][_BIN_BAND]
        p = (np.abs(X) ** 2) / _WPOW
        g = np.maximum(1 - NR_OVER * noise / np.maximum(p, 1e-14), NR_FLOOR)
        g = np.convolve(g, self.k3, mode="same")                 # wygładzenie w częstotliwości
        g = np.maximum(g, self.prev_g * 0.6)                      # wygładzenie w czasie
        self.prev_g = g
        return g * HP

    def process(self, x, final=False):
        buf = np.concatenate([self.inbuf, x])
        if final:
            buf = np.concatenate([buf, np.zeros(N_FFT, np.float32)])
        out = []
        i = 0
        while i + N_FFT <= len(buf):
            fr = buf[i:i + N_FFT] * _WIN
            X = np.fft.rfft(fr)
            X *= self._gain(X, self.frame_pos + N_FFT / 2)
            self.ola += np.fft.irfft(X, N_FFT).astype(np.float32) * _WIN
            out.append(self.ola[:HOP].copy())
            self.ola = np.concatenate([self.ola[HOP:], np.zeros(HOP, np.float32)])
            i += HOP
            self.frame_pos += HOP
        self.inbuf = buf[i:]
        y = np.concatenate(out) if out else np.zeros(0, np.float32)
        if self.skip:
            cut = min(self.skip, len(y))
            y = y[cut:]
            self.skip -= cut
        return y


def _soft_clip(y):
    a = np.abs(y)
    over = a > 0.9
    if over.any():
        y = y.copy()
        y[over] = np.sign(y[over]) * (0.9 + 0.1 * np.tanh((a[over] - 0.9) / 0.1))
    return y


def convert_to_opus(src, dst=None, bitrate=32000, progress=None, cancel=None, level=False, denoise=False):
    """
    Konwertuje src -> dst (.opus).
    level=True   – wyrównanie głośności (okno 0,8 s, z podglądem do przodu),
    denoise=True – redukcja szumu tła (filtr <80 Hz + bramka widmowa, maks. -15 dB).
    Przebiegi: 1) analiza poziomu i szumu, [2) przy obu opcjach: odszumienie i pomiar samej mowy],
    ostatni) zapis. Odszumianie jest deterministyczne, więc przebieg 2 i zapis dają ten sam sygnał.
    progress(ułamek 0..1), cancel() -> True przerywa. Przy błędzie usuwa niepełny plik.
    """
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".opus")
    if dst.resolve() == src.resolve():
        raise ValueError("Plik wejściowy i wyjściowy to ten sam plik")
    tmp = dst.with_name(dst.name + ".part")
    prog = progress or (lambda f: None)
    two_pass = level or denoise

    gain = centers = den = None
    if two_pass:
        # ── przebieg 1: poziom (i widmo w pasmach) każdej ramki 20 ms ──
        dbs, bands, rest = [], [], np.zeros(0, np.float32)
        p1 = 0.3 if (level and denoise) else 0.4
        for blk in _decode_mono(src, cancel, lambda f: prog(f * p1)):
            rest = np.concatenate([rest, blk])
            m = len(rest) // FRAME * FRAME
            if m:
                fr = rest[:m].reshape(-1, FRAME)
                dbs.append(10 * np.log10(np.mean(fr * fr, axis=1) + 1e-14))
                if denoise:
                    bands.append(_band_psd(fr))
                rest = rest[m:]
        db = np.concatenate(dbs) if dbs else np.zeros(0)
        active, floor = _activity(db) if len(db) else (np.zeros(0, bool), None)
        profile = _noise_profile(np.concatenate(bands), active) if (denoise and bands) else None
        if level and len(db):
            if profile is not None:
                # ── przebieg 2: poziom mowy PO odszumieniu ──
                d2, rest, dbs2 = _Denoiser(*profile), np.zeros(0, np.float32), []

                def measure(y):
                    nonlocal rest
                    rest = np.concatenate([rest, y])
                    m = len(rest) // FRAME * FRAME
                    if m:
                        fr = rest[:m].reshape(-1, FRAME)
                        dbs2.append(10 * np.log10(np.mean(fr * fr, axis=1) + 1e-14))
                        rest = rest[m:]
                for blk in _decode_mono(src, cancel, lambda f: prog(0.3 + f * 0.3)):
                    measure(d2.process(blk))
                measure(d2.process(np.zeros(0, np.float32), final=True))
                db2 = np.concatenate(dbs2) if dbs2 else db
                n = min(len(db2), len(active))
                # GDZIE jest mowa – z oryginału (pewne), JAK głośna – po odszumieniu
                gain = _gain_curve(db2[:n], active[:n], None, top=MAX_GAIN_NR)
            else:
                gain = _gain_curve(db, active)
            centers = np.arange(len(gain)) * FRAME + FRAME / 2
        if profile is not None:
            den = _Denoiser(*profile)

    # ── przebieg 2 (albo jedyny): zapis ──
    out = None
    try:
        out = av.open(str(tmp), "w", format="ogg")
        st = out.add_stream("libopus", rate=RATE, layout="mono")
        st.bit_rate = bitrate
        pos = 0          # próbki zapisane
        total_in = 0     # próbki wczytane (wyjście przycinamy do tej długości)

        def emit(y):
            nonlocal pos
            if not len(y):
                return
            if gain is not None:
                g = np.interp(np.arange(pos, pos + len(y)), centers, gain)
                y = y * (10 ** (g / 20)).astype(np.float32)
            if two_pass:
                y = _soft_clip(y)
            fr = av.AudioFrame.from_ndarray(
                (np.clip(y, -1, 1) * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono")
            fr.sample_rate = RATE
            fr.pts = pos
            pos += len(y)
            for pkt in st.encode(fr):
                out.mux(pkt)

        p0, pw = ((0.6, 0.4) if (level and denoise) else (0.4, 0.6)) if two_pass else (0.0, 1.0)
        for blk in _decode_mono(src, cancel, lambda f: prog(p0 + f * pw)):
            total_in += len(blk)
            emit(den.process(blk) if den else blk)
        if den:
            emit(den.process(np.zeros(0, np.float32), final=True)[:max(0, total_in - pos)])
        for pkt in st.encode(None):
            out.mux(pkt)
        out.close()
        tmp.replace(dst)
    except BaseException:
        try:
            if out:
                out.close()
        except Exception:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    prog(1.0)
    return dst


def verify(src, dst, tolerance=2.0):
    """True, jeśli długości plików różnią się o mniej niż `tolerance` sekund."""
    return abs(probe_duration(src) - probe_duration(dst)) < tolerance
