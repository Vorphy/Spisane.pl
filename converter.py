"""
converter.py – konwersja dowolnego pliku audio/wideo do Opus mono (PyAV, bez ffmpeg),
opcjonalnie z WYRÓWNANIEM GŁOŚNOŚCI i REDUKCJĄ SZUMU (dwa przebiegi, z podglądem do przodu).

Wyrównanie: plik dzielony na ramki 20 ms; dla każdej ramki liczymy poziom mowy w oknie ±0,4 s
(tylko ramki ponad szumem tła) i wzmocnienie dociągające go do TARGET. Długie przerwy (bez mowy
w oknie) zostają nietknięte – szum nie jest podgłaśniany. Pamięć: tylko krzywa wzmocnienia
(~180 tys. liczb na godzinę), nie całe audio.
"""

from datetime import datetime
from pathlib import Path
import numpy as np
import av

# Znacznik obróbki zapisywany w pliku Opus (metadane strumienia) – żeby nie obrabiać dwa razy.
TAG_STEPS = "NOTATNIK_OBROBKA"     # np. "level,denoise"
TAG_WHEN = "NOTATNIK_KIEDY"        # np. "2026-11-09 11:26"
TAG_SOURCE = "NOTATNIK_ZRODLO"     # "nagrywanie" / "konwersja"


def read_processing(path):
    """Co już zrobiono z plikiem: {"steps": {"level", "denoise"}, "when": str, "source": str} albo None."""
    try:
        with av.open(str(path)) as c:
            md = {}
            for s in c.streams.audio[:1]:
                md.update({k.upper(): v for k, v in s.metadata.items()})
            md.update({k.upper(): v for k, v in c.metadata.items()})
    except Exception:
        return None
    steps = {x for x in md.get(TAG_STEPS, "").split(",") if x in ("level", "denoise")}
    if not steps:
        return None
    return {"steps": steps, "when": md.get(TAG_WHEN, ""), "source": md.get(TAG_SOURCE, "")}

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


def channel_count(path):
    try:
        with av.open(str(path)) as c:
            a = c.streams.audio[0]
            return a.codec_context.channels or a.channels
    except Exception:
        return 0


def _decode(path, cancel=None, progress=None, channel=None, block=RATE):
    """Jak _decode_frames, ale w blokach ~1 s – obróbka wsadowa (FFT, wykrywanie mowy) jest wtedy
    kilka razy szybsza niż na kawałkach po 20 ms prosto z dekodera."""
    buf, n = [], 0
    for x in _decode_frames(path, cancel, progress, channel):
        buf.append(x)
        n += x.shape[-1]
        if n >= block:
            yield np.concatenate(buf, axis=-1)
            buf, n = [], 0
    if buf:
        yield np.concatenate(buf, axis=-1)


def _decode_frames(path, cancel=None, progress=None, channel=None):
    """Generator bloków float32 48 kHz.
    channel=None – mono (kanały zmiksowane), 0/1 – jeden kanał pliku stereo, "both" – tablica (2, n)."""
    stereo = channel is not None
    with av.open(str(path)) as inp:
        if not inp.streams.audio:
            raise ValueError("Plik nie zawiera ścieżki dźwiękowej")
        ain = inp.streams.audio[0]
        total = (inp.duration / 1_000_000) if inp.duration else None
        rs = av.AudioResampler(format="s16", layout="stereo" if stereo else "mono", rate=RATE)
        last = -1.0

        def conv(rf):
            x = rf.to_ndarray().reshape(-1).astype(np.float32) / 32768
            if not stereo:
                return x
            x = x.reshape(-1, 2).T               # s16 stereo (przeplot) -> (2, n)
            return x if channel == "both" else x[channel]

        for frame in inp.decode(ain):
            if cancel and cancel():
                raise InterruptedError("Przerwano")
            for rf in rs.resample(frame):
                yield conv(rf)
            if progress and total and frame.time is not None:
                f = min(1.0, frame.time / total)
                if f - last >= 0.01:
                    progress(f)
                    last = f
        for rf in rs.resample(None):
            yield conv(rf)


def _decode_mono(path, cancel=None, progress=None):
    return _decode(path, cancel, progress)


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

    def process(self, x, final=False):
        """Wszystkie pełne ramki bloku naraz (FFT wsadowo), wynik identyczny z liczeniem ramka po ramce."""
        buf = np.concatenate([self.inbuf, x])
        if final:
            buf = np.concatenate([buf, np.zeros(N_FFT, np.float32)])
        F = (len(buf) - N_FFT) // HOP + 1 if len(buf) >= N_FFT else 0
        if F:
            fr = np.lib.stride_tricks.sliding_window_view(buf, N_FFT)[::HOP][:F] * _WIN
            X = np.fft.rfft(fr, axis=1)
            # profil szumu dla środka każdej ramki
            centers = self.frame_pos + np.arange(F) * HOP + N_FFT / 2
            blk = np.clip((centers // FRAME).astype(int) // self.step, 0, len(self.profile) - 1)
            noise = self.profile[blk][:, _BIN_BAND]
            p = (np.abs(X) ** 2) / _WPOW
            g = np.maximum(1 - NR_OVER * noise / np.maximum(p, 1e-14), NR_FLOOR)
            gp = np.pad(g, ((0, 0), (2, 2)))                       # wygładzenie w częstotliwości (jak convolve "same")
            g = (gp[:, :-4] + gp[:, 1:-3] + gp[:, 2:-2] + gp[:, 3:-1] + gp[:, 4:]) * self.k3[0]
            prev = self.prev_g
            for t in range(F):                                     # wygładzenie w czasie (rekurencyjne)
                prev = np.maximum(g[t], prev * 0.6)
                g[t] = prev
            self.prev_g = prev
            Y = np.fft.irfft(X * (g * HP), N_FFT, axis=1).astype(np.float32) * _WIN
            # nakładanie z przesunięciem N_FFT/2: wyjście k = ogon ramki k-1 + początek ramki k
            tails = np.concatenate([self.ola[None, :HOP], Y[:-1, HOP:]])
            y = (tails + Y[:, :HOP]).reshape(-1)
            self.ola = np.concatenate([Y[-1, HOP:], np.zeros(HOP, np.float32)])
            self.frame_pos += F * HOP
        else:
            y = np.zeros(0, np.float32)
        self.inbuf = buf[F * HOP:]
        if self.skip:
            cut = min(self.skip, len(y))
            y = y[cut:]
            self.skip -= cut
        return y


# ── Bramka mowy (ścieżka mikrofonu) ────────────────────────────────────────
# Silero VAD (z faster-whisper) ocenia co 32 ms, czy to mowa. Poza mową mikrofon jest ściszany
# o GATE_DB – znikają oddechy, siorbanie, stukanie, a rozmówcy na drugiej ścieżce zostają czyści.
GATE_DB = -40.0               # tłumienie poza mową
GATE_ON, GATE_OFF = 0.5, 0.35 # progi prawdopodobieństwa mowy (histereza)
GATE_MIN = 10                 # ramek = 0,2 s – krótsze „wypowiedzi” to odgłosy, nie mowa
GATE_PRE, GATE_POST = 8, 15   # ramek = 0,16 s przed / 0,3 s po mowie zostaje (początki i końcówki słów)
VAD_N = 512                   # próbek 16 kHz = 32 ms – krok modelu


class _SpeechDetector:
    """Zbiera sygnał 48 kHz, liczy prawdopodobieństwo mowy (Silero) co 32 ms."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model   # import tu – tylko gdy bramka włączona
        self.model = get_vad_model()
        self.rest = np.zeros(0, np.float32)
        self.buf, self.probs = [], []
        self.n = 0                                  # próbek 16 kHz w self.buf

    def feed(self, x, final=False):
        self.rest = np.concatenate([self.rest, x])
        m = len(self.rest) // 3 * 3
        if m:
            self.buf.append(self.rest[:m].reshape(-1, 3).mean(axis=1))   # 48 → 16 kHz
            self.n += m // 3
            self.rest = self.rest[m:]
        if final or self.n >= 16000 * 60:                                 # model po minucie
            a = np.concatenate(self.buf) if self.buf else np.zeros(0, np.float32)
            self.buf, self.n = [], 0
            if final and len(a) % VAD_N:
                a = np.pad(a, (0, VAD_N - len(a) % VAD_N))
            m = len(a) // VAD_N * VAD_N
            if m:
                self.probs.append(np.asarray(self.model(a[:m])).reshape(-1))
            if len(a) > m:
                self.buf, self.n = [a[m:]], len(a) - m

    def gate(self, n_frames):
        """-> tłumienie dB na ramkę 20 ms (0 = mowa, GATE_DB = reszta)."""
        self.feed(np.zeros(0, np.float32), final=True)
        p = np.concatenate(self.probs) if self.probs else np.zeros(0)
        idx = np.minimum(((np.arange(n_frames) + 0.5) * FRAME / 3 / VAD_N).astype(int), max(len(p) - 1, 0))
        pf = p[idx] if len(p) else np.zeros(n_frames)
        speech = np.zeros(n_frames, bool)
        on = False
        for i, v in enumerate(pf):                 # histereza
            on = v >= GATE_ON or (on and v >= GATE_OFF)
            speech[i] = on
        # krótkie wyskoki (cmoknięcie, łyk) – precz
        d = np.diff(np.concatenate([[0], speech.astype(np.int8), [0]]))
        for s, e in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
            if e - s < GATE_MIN:
                speech[s:e] = False
        # margines przed i po mowie
        k = np.convolve(speech.astype(float), np.ones(GATE_PRE + GATE_POST + 1), mode="full")
        keep = k[GATE_PRE:GATE_PRE + n_frames] > 0   # mowa w [i-POST, i+PRE]
        g = np.where(keep, 0.0, GATE_DB)
        return np.convolve(np.pad(g, 2, mode="edge"), np.ones(5) / 5, mode="valid")   # łagodne wejście/wyjście


def _soft_clip(y):
    a = np.abs(y)
    over = a > 0.9
    if over.any():
        y = y.copy()
        y[over] = np.sign(y[over]) * (0.9 + 0.1 * np.tanh((a[over] - 0.9) / 0.1))
    return y


class _Levels:
    """Poziom (dB) ramek 20 ms ze strumienia bloków dowolnej długości."""

    def __init__(self, bands=False):
        self.rest, self.dbs, self.bands, self.want_bands = np.zeros(0, np.float32), [], [], bands

    def feed(self, y):
        self.rest = np.concatenate([self.rest, y])
        m = len(self.rest) // FRAME * FRAME
        if m:
            fr = self.rest[:m].reshape(-1, FRAME)
            self.dbs.append(10 * np.log10(np.mean(fr * fr, axis=1) + 1e-14))
            if self.want_bands:
                self.bands.append(_band_psd(fr))
            self.rest = self.rest[m:]

    @property
    def db(self):
        return np.concatenate(self.dbs) if self.dbs else np.zeros(0)


def _analyze(src, level, denoise, cancel, prog, chans=(None,), gates=None):
    """Przebiegi analizy -> [(gain, centers, profile)] dla każdego sygnału z chans
    ([None] = mono, [0, 1] = oba kanały pliku stereo – dekodowane RAZ na przebieg, nie osobno).
    gates[k]=True – bramka mowy (Silero VAD) dopisana do krzywej wzmocnienia k: poza mową GATE_DB.
    prog(ułamek 0..1) – postęp samej analizy."""
    split = list(chans) != [None]
    mode = "both" if split else None
    n = len(chans)
    gates = gates or [False] * n
    lv = [_Levels(bands=denoise) for _ in range(n)]
    dets = []
    for g in gates:
        try:
            dets.append(_SpeechDetector() if g else None)
        except Exception:       # brak faster-whisper/onnxruntime – obróbka bez bramki
            dets.append(None)
    two = level and denoise
    p1 = 0.5 if two else 1.0
    for blk in _decode(src, cancel, lambda f: prog(f * p1), mode):
        for k, x in enumerate(blk if split else [blk]):
            if dets[k]:
                dets[k].feed(x)
            lv[k].feed(x)

    st = []
    for k in range(n):
        db = lv[k].db
        active = _activity(db)[0] if len(db) else np.zeros(0, bool)
        profile = _noise_profile(np.concatenate(lv[k].bands), active) if (denoise and lv[k].bands) else None
        gate_db = dets[k].gate(len(db)) if dets[k] and len(db) else None
        lvl_active = active & (gate_db > GATE_DB / 2) if gate_db is not None else active   # poziom tylko z mowy
        st.append({"db": db, "active": lvl_active, "profile": profile, "gate": gate_db, "gain": None})

    if level:
        need = [k for k in range(n) if len(st[k]["db"]) and st[k]["profile"] is not None]
        if need:
            # ── poziom mowy PO odszumieniu (wszystkie kanały w jednym przebiegu) ──
            dens = {k: _Denoiser(*st[k]["profile"]) for k in need}
            lv2 = {k: _Levels() for k in need}
            for blk in _decode(src, cancel, lambda f: prog(0.5 + f * 0.5), mode):
                sigs = blk if split else [blk]
                for k in need:
                    lv2[k].feed(dens[k].process(sigs[k]))
            for k in need:
                lv2[k].feed(dens[k].process(np.zeros(0, np.float32), final=True))
                db2 = lv2[k].db if lv2[k].dbs else st[k]["db"]
                m = min(len(db2), len(st[k]["active"]))
                # GDZIE jest mowa – z oryginału (pewne), JAK głośna – po odszumieniu
                st[k]["gain"] = _gain_curve(db2[:m], st[k]["active"][:m], None, top=MAX_GAIN_NR)
        for k in range(n):
            if st[k]["gain"] is None and len(st[k]["db"]):
                st[k]["gain"] = _gain_curve(st[k]["db"], st[k]["active"])

    out = []
    for s_ in st:
        gain, centers = s_["gain"], None
        if s_["gate"] is not None:
            gain = s_["gate"] if gain is None else gain + s_["gate"][:len(gain)]
        if gain is not None:
            centers = np.arange(len(gain)) * FRAME + FRAME / 2
        out.append((gain, centers, s_["profile"]))
    return out


def convert_to_opus(src, dst=None, bitrate=32000, progress=None, cancel=None, level=False, denoise=False,
                    source="konwersja", split_channels=False, gate=False):
    """
    Konwertuje src -> dst (.opus, mono).
    level=True   – wyrównanie głośności (okno 0,8 s, z podglądem do przodu),
    denoise=True – redukcja szumu tła (filtr <80 Hz + bramka widmowa, maks. -15 dB).
    split_channels=True i plik stereo (nagranie: lewy = mikrofon, prawy = rozmówcy) – każdy kanał
    analizowany i obrabiany osobno (własny profil szumu, własne wyrównanie), potem miks do mono.
    gate=True (tylko z split_channels) – bramka mowy na kanale mikrofonu: oddechy, siorbanie itp. ściszone.
    Przebiegi: analiza poziomu i szumu [+ pomiar mowy po odszumieniu] dla każdego sygnału, na końcu zapis.
    Odszumianie jest deterministyczne, więc pomiar i zapis dają ten sam sygnał.
    progress(ułamek 0..1), cancel() -> True przerywa. Przy błędzie usuwa niepełny plik.
    Wykonane kroki (razem z wcześniejszymi ze znacznika źródła) zapisuje w znaczniku pliku wynikowego.
    """
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".opus")
    if dst.resolve() == src.resolve():
        raise ValueError("Plik wejściowy i wyjściowy to ten sam plik")
    tmp = dst.with_name(dst.name + ".part")
    prog = progress or (lambda f: None)
    split = split_channels and channel_count(src) == 2
    gate = gate and split
    two_pass = level or denoise or gate
    chans = [0, 1] if split else [None]

    # ── analiza (0–60% postępu przy obróbce) ──
    plans = []
    if two_pass:
        res = _analyze(src, level, denoise, cancel, lambda f: prog(0.6 * f), chans,
                       gates=[gate and ch == 0 for ch in chans])
        for g, c, prof in res:
            plans.append({"gain": g, "centers": c, "den": _Denoiser(*prof) if prof is not None else None, "pos": 0})
    else:
        plans = [{"gain": None, "centers": None, "den": None, "pos": 0} for _ in chans]

    # ── zapis ──
    out = None
    try:
        out = av.open(str(tmp), "w", format="ogg")
        st = out.add_stream("libopus", rate=RATE, layout="mono")
        st.bit_rate = bitrate
        prev = read_processing(src)
        steps = (prev["steps"] if prev else set()) | {x for x, on in (("level", level), ("denoise", denoise)) if on}
        if steps:
            st.metadata[TAG_STEPS] = ",".join(sorted(steps))
            st.metadata[TAG_WHEN] = (prev["when"] if prev and not (level or denoise)
                                     else f"{datetime.now():%Y-%m-%d %H:%M}")
            st.metadata[TAG_SOURCE] = prev["source"] if prev and not (level or denoise) else source
        pos = 0          # próbki zapisane
        total_in = 0     # próbki wczytane (wyjście przycinamy do tej długości)
        pend = [np.zeros(0, np.float32) for _ in chans]   # wyjścia kanałów czekające na wspólną długość

        def shape(pl, y):
            if pl["gain"] is not None and len(y):
                g = np.interp(np.arange(pl["pos"], pl["pos"] + len(y)), pl["centers"], pl["gain"])
                y = y * (10 ** (g / 20)).astype(np.float32)
            pl["pos"] += len(y)
            return y

        def write(y):
            nonlocal pos
            y = y[:max(0, total_in - pos)]
            if not len(y):
                return
            if two_pass or split:
                y = _soft_clip(y)
            fr = av.AudioFrame.from_ndarray(
                (np.clip(y, -1, 1) * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono")
            fr.sample_rate = RATE
            fr.pts = pos
            pos += len(y)
            for pkt in st.encode(fr):
                out.mux(pkt)

        def feed(ys):
            for k, y in enumerate(ys):
                pend[k] = np.concatenate([pend[k], shape(plans[k], y)])
            n = min(len(x) for x in pend)
            if n:
                mix = pend[0][:n] if len(pend) == 1 else pend[0][:n] + pend[1][:n]
                for k in range(len(pend)):
                    pend[k] = pend[k][n:]
                write(mix)

        p0, pw = (0.6, 0.4) if two_pass else (0.0, 1.0)
        for blk in _decode(src, cancel, lambda f: prog(p0 + f * pw), "both" if split else None):
            sigs = [blk[0], blk[1]] if split else [blk]
            total_in += len(sigs[0])
            feed([pl["den"].process(x) if pl["den"] else x for pl, x in zip(plans, sigs)])
        if any(pl["den"] for pl in plans):
            z = np.zeros(0, np.float32)
            feed([pl["den"].process(z, final=True) if pl["den"] else z for pl in plans])
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
