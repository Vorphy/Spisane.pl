"""
engine.py – silnik transkrypcji (dawniej fw-dd.py), bez interfejsu konsolowego.

Whisper (domyślnie large-v3-turbo):
  - igpu: grafika Intel Iris Xe przez OpenVINO (int8, beam 1) + weryfikacja VAD
          (ponowna transkrypcja fragmentów pominiętych przez model),
          a gdy grafika niedostępna – automatycznie CPU,
  - cpu / cuda: faster-whisper (typ obliczeń, beam size, wątki, rdzenie P/E).
Zapis na bieżąco (txt / srt / vtt), filtr VAD, wznawianie przerwanych transkrypcji
(plik .transcribe_state), wirtualne przycinanie czasu (limit_time).

Używany przez worker.py (osobny proces) – patrz TranscribeTab w notatnik.pyw.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus",
                    ".aac", ".wma", ".webm", ".mp4", ".mkv"}

MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v1", "large-v2", "large-v3"]
MODEL_INFO = {
    "tiny": "najszybszy, najmniej dokładny (~75 MB)",
    "base": "szybki, podstawowa jakość (~150 MB)",
    "small": "zoptymalizowany pod CPU PL (~500 MB)",
    "medium": "dobra jakość (~1.5 GB)",
    "large-v3-turbo": "jakość large, bardzo szybki (~1.5 GB)",
    "large-v1": "pierwszy large (~3 GB)",
    "large-v2": "najlepsza jakość, wolny na CPU (~3 GB)",
    "large-v3": "najnowszy large (~3 GB)",
}
COMPUTE_TYPES = ["int8", "int8_float16", "float16", "float32"]
FORMATS = ["txt", "srt", "vtt", "all"]

# Domyślne opcje = domyślne wartości CLI fw-dd.py
DEFAULT_OPTIONS = {
    "model": "large-v3-turbo",
    "language": "pl",
    "device": "auto",          # auto / igpu / vulkan / cpu / cuda
    "compute_type": "int8",    # nie dotyczy igpu (tam zawsze int8)
    "threads": 8,
    "cores": "e",              # e / p / all / liczba N
    "format": "txt",
    "beam_size": 2,            # igpu zawsze 1
    "timestamps": True,
    "vad": True,
    "limit_time": "",          # np. "45" (minut) albo "01:15:00"; pusto = bez przycinania
    "vocab": "",               # słownik nazw (hotwords) – trzymany tylko w config.json, nie w repozytorium
    "vocab_on": True,
}

# Słownik nazw: Whisper przyjmuje najwyżej 223 tokeny podpowiedzi (max_length // 2 - 1).
# Test 2026-11-10 (ręczna transkrypcja, lista 196 tokenów z Confluence): WER grafika 24,4% -> 21,1%,
# procesor beam 2 26,1% -> 22,9%; nazwy z listy we wzorcu 2 -> 8 z 9, bez wstawiania nazw, których nie mówiono.
# hotwords i initial_prompt dały to samo; hotwords trafia odrobinę więcej nazw. Dopisywanie odmian nic nie daje.
VOCAB_MAX_TOKENS = 223
_tokenizer = None


def vocab_tokens(text):
    """Liczba tokenów Whispera dla słownika (jak liczy faster-whisper: ' ' + tekst).
    Bez pobranego tokenizera – przybliżenie."""
    global _tokenizer
    text = (text or "").strip()
    if not text:
        return 0
    if _tokenizer is None:
        try:
            import tokenizers
            from huggingface_hub import try_to_load_from_cache
            path = try_to_load_from_cache("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "tokenizer.json")
            _tokenizer = tokenizers.Tokenizer.from_file(path) if isinstance(path, str) else False
        except Exception:
            _tokenizer = False
    if _tokenizer:
        return len(_tokenizer.encode(" " + text, add_special_tokens=False).ids)
    return round(len(text) / 2.6)   # polski tekst z nazwami: ok. 2,6 znaku na token

# Opcje wymagające ponownego załadowania modelu (reszta działa per plik)
MODEL_KEYS = ("model", "device", "compute_type", "threads", "cores")


def fmt_elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def parse_time_to_seconds(time_str):
    """Konwertuje czas (minuty lub HH:MM:SS / MM:SS) na sekundy. None = brak / zły format."""
    time_str = (time_str or "").strip()
    if not time_str:
        return None
    try:
        if ":" in time_str:
            parts = [float(x) for x in time_str.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            return None
        return float(time_str) * 60.0
    except ValueError:
        return None


def parse_cores(value):
    v = str(value).strip().lower()
    if v in ("p", "e", "all") or (v.isdigit() and int(v) >= 1):
        return v
    raise ValueError("rdzenie: dozwolone p, e, all albo liczba (np. 6)")


# ── Rdzenie CPU (P / E) ──────────────────────────────────────────────────
def detect_core_types():
    """Zwraca (lista_cpu_P, lista_cpu_E) – numery procesorów logicznych wg typu rdzenia.
    Na procesorach bez podziału P/E (lub poza Windows) wszystkie trafiają do listy P."""
    all_cpus = list(range(os.cpu_count() or 1))
    if sys.platform != "win32":
        return all_cpus, []
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        size = wintypes.ULONG(0)
        k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(size), None, 0)
        buf = (ctypes.c_ubyte * size.value)()
        if not k32.GetSystemCpuSetInformation(buf, size, ctypes.byref(size), None, 0):
            return all_cpus, []
        raw = bytes(buf)
        cpus = []
        off = 0
        while off < size.value:
            entry_size = int.from_bytes(raw[off:off + 4], "little")
            # SYSTEM_CPU_SET_INFORMATION: LogicalProcessorIndex @14, EfficiencyClass @18
            cpus.append((raw[off + 14], raw[off + 18]))
            off += entry_size
        top = max(eff for _, eff in cpus)
        return (sorted(lp for lp, eff in cpus if eff == top),
                sorted(lp for lp, eff in cpus if eff != top))
    except Exception:
        return all_cpus, []


def cores_mask(cores):
    """('p' | 'e' | 'all' | N) -> (maska bitowa albo None = wszystkie, opis)."""
    p, e = detect_core_types()
    all_cpus = sorted(p + e)
    if cores == "all":
        return None, f"wszystkie ({len(all_cpus)} CPU)"
    if cores == "p":
        selected, label = p, "tylko P (wydajne)"
    elif cores == "e":
        if not e:
            return None, "wszystkie (procesor nie ma rdzeni E)"
        selected, label = e, "tylko E (energooszczędne)"
    else:
        n = max(1, min(int(cores), len(all_cpus)))
        selected, label = (p + e)[:n], f"pierwsze {n} CPU"
    return sum(1 << c for c in selected), f"{label}: CPU {','.join(map(str, selected))}"


def no_power_throttling(handle):
    """Wyłącza dla procesu tryb wydajności Windows 11 (EcoQoS). Windows sam go włącza procesom w tle
    bez okna i z niskim priorytetem: niskie taktowanie i długie przestoje, obróbka szła ~2× wolniej.
    Niski priorytet zostaje, więc okno i nagrywanie nadal mają pierwszeństwo."""
    if os.name != "nt" or handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _PPT(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        st = _PPT(1, 1, 0)       # PROCESS_POWER_THROTTLING_EXECUTION_SPEED: kontrolowane, wyłączone
        k32 = ctypes.windll.kernel32
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        return bool(k32.SetProcessInformation(int(handle), 4, ctypes.byref(st), ctypes.sizeof(st)))  # 4 = ProcessPowerThrottling
    except Exception:
        return False


def set_affinity(cores, handle=None):
    """Przypina proces (domyślnie BIEŻĄCY; albo uchwyt innego procesu) do rdzeni:
    'p', 'e', 'all' albo liczba N (pierwsze N procesorów logicznych – najpierw P, potem E).
    Działa też na już pracującym procesie. Zwraca opis."""
    mask, desc = cores_mask(cores)
    if mask is None:
        mask = sum(1 << c for c in range(os.cpu_count() or 1))
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        k32.SetProcessAffinityMask.restype = wintypes.BOOL
        h = k32.GetCurrentProcess() if handle is None else int(handle)
        if not k32.SetProcessAffinityMask(h, mask):
            raise OSError("SetProcessAffinityMask nie powiodło się")
    except Exception as ex:
        return f"wszystkie (nie udało się przypiąć: {ex})"
    return desc


def apply_cores(cores):
    """Przypina BIEŻĄCY proces do rdzeni (zob. set_affinity). Zwraca opis."""
    if cores == "all":
        return cores_mask("all")[1]
    return set_affinity(cores)


def core_summary():
    """Do okna opcji: ile jest rdzeni P i E."""
    p, e = detect_core_types()
    return len(p), len(e)


# ── Monitor folderu: czy plik skończył się zapisywać ─────────────────────
def _live_size(path):
    """Rozmiar z otwartego uchwytu (os.path.getsize na Windows bywa nieaktualny podczas zapisu).
    None = pliku nie da się otworzyć (blokada wyłączna = trwa zapis)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            return f.tell()
    except OSError:
        return None


def _is_unlocked(path):
    try:
        with open(path, "rb+"):
            return True
    except OSError:
        return False


class WriteWatcher:
    """Nieblokujący odpowiednik wait_for_file_copy_complete z fw-dd.py:
    plik jest gotowy, gdy przez `cycles` kolejnych sprawdzeń (co `interval` s)
    nie zmienia się ani rozmiar, ani mtime, i nie jest zablokowany."""

    def __init__(self, path, interval=5.0, cycles=3):
        self.path = Path(path)
        self.interval = interval
        self.cycles = cycles
        self.last = (-1, -1.0)
        self.stable = 0
        self.next_check = 0.0
        self.note = "czekam na koniec zapisu…"

    def ready(self):
        now = time.time()
        if now < self.next_check:
            return False
        self.next_check = now + self.interval
        size = _live_size(self.path)
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = self.last[1]
        if size is None:
            self.stable, self.note = 0, "plik zablokowany (trwa zapis)…"
        elif size == 0:
            self.stable, self.note = 0, "plik pusty – czekam…"
        elif (size, mtime) == self.last:
            self.stable += 1
            self.note = f"rozmiar stabilny {size / 1048576:.1f} MB [{self.stable}/{self.cycles}]"
        else:
            self.stable, self.note = 0, f"plik nadal rośnie: {size / 1048576:.1f} MB"
        if size is not None:
            self.last = (size, mtime)
        if self.stable >= self.cycles:
            if _is_unlocked(self.path):
                return True
            self.stable, self.note = 0, "rozmiar stabilny, ale plik nadal zablokowany…"
        return False


# ── Stan do wznawiania ───────────────────────────────────────────────────
def state_path(audio_path):
    return Path(audio_path).with_suffix(".transcribe_state")


def save_state(audio_path, last_sec, segments, limit_time=None):
    data = {
        "last_checkpoint_seconds": last_sec,
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
        "limit_time": limit_time,
        "timestamp": time.time(),
    }
    with open(state_path(audio_path), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_state(audio_path):
    p = state_path(audio_path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class Segment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


# ── Transkrypcja porcjami (wspólne dla grafiki Intel i faster-whisper) ───────────────
class PortionWhisper:
    """Udaje interfejs WhisperModel.transcribe(). Mowę (po VAD) dzieli na porcje najwyżej jednego okna Whispera,
    cięte zawsze w ciszy, i transkrybuje każdą osobno; potem sprawdza miejsca podejrzane o pominięcie (VAD).
    Podklasy dostarczają _config() i _generate()."""

    SR = 16000
    # Ile sekund mowy transkrybować naraz: najwyżej jedno okno Whispera (30 s), cięte zawsze w ciszy (VAD).
    # Dłuższe porcje model dzieli sam na okna 30 s w przypadkowych miejscach i gubi całe wypowiedzi.
    # Test 2026-11-10 z ręczną transkrypcją (3 min), WER / pominięte słowa:
    #   grafika: porcje 120 s 30,9% / 58 -> 28 s 24,4% / 22 (czas +14%);
    #   procesor beam 2: cała mowa naraz (faster-whisper) 29,1% / 51 -> 28 s 26,1% / 28 (czas bez zmian).
    PORTION_S = 28
    RETRY_S = 5            # mowa (po VAD) tak długa bez tekstu albo z za małą liczbą słów = transkrybuj ponownie
    MIN_WORDS_PER_S = 0.5  # normalna mowa to ok. 2 słowa/s

    beam_size = 1
    hotwords = None

    def transcribe(self, audio, language="pl", vad_filter=True, initial_prompt=None, beam_size=1, hotwords=None,
                   **_ignored):
        from types import SimpleNamespace
        from faster_whisper import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self.beam_size = beam_size
        self.hotwords = (hotwords or "").strip() or None   # słownik nazw – do każdej porcji
        if isinstance(audio, str):
            audio = decode_audio(audio, sampling_rate=self.SR)
        if vad_filter:
            chunks = get_speech_timestamps(audio, VadOptions())
        else:
            chunks = [{"start": 0, "end": len(audio)}]
        info = SimpleNamespace(duration=len(audio) / self.SR)
        return self._segments(audio, chunks, language, initial_prompt), info

    def _config(self, language, initial_prompt):
        raise NotImplementedError

    def _generate(self, piece, cfg, offset):
        """-> lista (start, koniec, tekst) w sekundach, przesunięta o offset"""
        raise NotImplementedError

    def _segments(self, audio, chunks, language, initial_prompt):
        import numpy as np
        # Fragmenty mowy z VAD łączymy w porcje do PORTION_S sekund (cięcie zawsze w ciszy)
        portions, current, current_len = [], [], 0
        for c in chunks:
            n = c["end"] - c["start"]
            if current and current_len + n > self.PORTION_S * self.SR:
                portions.append(current)
                current, current_len = [], 0
            current.append(c)
            current_len += n
        if current:
            portions.append(current)

        for i, portion in enumerate(portions):
            cfg = self._config(language, initial_prompt if i == 0 else None)
            piece = np.concatenate([audio[c["start"]:c["end"]] for c in portion])
            segs = self._generate(piece, cfg, 0.0)
            segs = self._verify_with_vad(piece, segs, cfg, portion)
            for start, end, text in segs:
                yield Segment(self._to_original_time(start, portion),
                              self._to_original_time(end, portion), " " + text)

    def _verify_with_vad(self, piece, segs, cfg, portion):
        """Porcja zawiera już tylko mowę, więc fragment bez tekstu albo segment z podejrzanie małą
        liczbą słów oznacza, że model przeskoczył fragment.
        Takie miejsca transkrybujemy ponownie i bierzemy wynik, jeśli ma więcej słów."""
        words = lambda ss: sum(len(re.findall(r"\w+", s[2])) for s in ss)
        piece_end = len(piece) / self.SR
        suspects = []
        bounds = [0.0] + [t for s in segs for t in (s[0], s[1])] + [piece_end]
        for gap_start, gap_end in zip(bounds[0::2], bounds[1::2]):
            if gap_end - gap_start >= self.RETRY_S:
                suspects.append((gap_start, gap_end, []))
        for s in segs:
            if s[1] - s[0] >= self.RETRY_S and words([s]) / (s[1] - s[0]) < self.MIN_WORDS_PER_S:
                suspects.append((s[0], s[1], [s]))

        for start, end, old in suspects:
            a = max(0, int((start - 0.5) * self.SR))
            b = min(len(piece), int((end + 0.5) * self.SR))
            found = [s for s in self._generate(piece[a:b], cfg, a / self.SR) if start - 1.0 <= s[0] < end]
            t0 = self._to_original_time(start, portion)
            better = words(found) > words(old)
            self.log(f"[VAD] {int(t0 // 60)}:{int(t0 % 60):02d} – {end - start:.0f}s mowy, {words(old)} słów; "
                     f"ponownie: {words(found)} słów{' (przyjęto)' if better else ' (bez zmian)'}")
            if better:
                segs = [s for s in segs if s not in old] + found
        return sorted(segs)

    def _to_original_time(self, t, portion):
        """Czas w sklejonej porcji (bez ciszy) -> czas w oryginalnym nagraniu."""
        pos = max(0.0, t) * self.SR
        for c in portion:
            n = c["end"] - c["start"]
            if pos <= n:
                return (c["start"] + pos) / self.SR
            pos -= n
        return portion[-1]["end"] / self.SR


# ── Whisper na grafice Intel (OpenVINO) ──────────────────────────────────
class IGPUWhisper(PortionWhisper):
    """Whisper na zintegrowanej grafice Intel (Iris Xe) przez OpenVINO.
    Model (OpenVINO/whisper-<model>-int8-ov) pobiera się przy pierwszym użyciu."""

    def __init__(self, model_name, log=print):
        import openvino_genai as ov_genai
        from huggingface_hub import snapshot_download
        self.log = log
        path = snapshot_download(f"OpenVINO/whisper-{model_name}-int8-ov")
        # Pamięć podręczna skompilowanego modelu – kolejne starty trwają sekundy, nie ~40 s
        cache_dir = Path.home() / ".cache" / "fw-openvino"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.pipe = ov_genai.WhisperPipeline(path, "GPU", CACHE_DIR=str(cache_dir))

    def _config(self, language, initial_prompt):
        cfg = self.pipe.get_generation_config()
        cfg.language = f"<|{language}|>"
        cfg.task = "transcribe"
        cfg.return_timestamps = True
        cfg.num_beams = 1  # beam search na GPU nie jest jeszcze obsługiwany przez OpenVINO (CVS-195124)
        if initial_prompt:
            cfg.initial_prompt = initial_prompt
        if self.hotwords:
            cfg.hotwords = self.hotwords
        return cfg

    def _generate(self, piece, cfg, offset):
        result = self.pipe.generate(piece.tolist(), cfg)
        piece_end = len(piece) / self.SR
        segs = []
        for ch in result.chunks:
            text = ch.text.strip()
            if not text:
                continue
            end_ts = ch.end_ts if ch.end_ts is not None and ch.end_ts >= 0 else piece_end
            segs.append((offset + max(0.0, ch.start_ts), offset + end_ts, text))
        return segs


# ── faster-whisper (procesor / NVIDIA) porcjami ──────────────────────────
class FasterWhisperPortions(PortionWhisper):
    """faster-whisper z tym samym podziałem na porcje co grafika. Bez tego faster-whisper skleja całą mowę
    i przesuwa okno 30 s według znaczników czasu – w długich wypowiedziach gubił całe fragmenty
    (test z ręczną transkrypcją: WER 29,1% -> 26,1%, pominięte słowa 51 -> 28, ten sam czas)."""

    def __init__(self, model, log=print):
        self.model = model
        self.log = log

    def _config(self, language, initial_prompt):
        return {"language": language, "initial_prompt": initial_prompt}

    def _generate(self, piece, cfg, offset):
        segs, _ = self.model.transcribe(piece, language=cfg["language"], beam_size=self.beam_size,
                                        vad_filter=False, condition_on_previous_text=False,
                                        initial_prompt=cfg["initial_prompt"], hotwords=self.hotwords)
        return [(offset + sg.start, offset + sg.end, sg.text.strip()) for sg in segs if sg.text.strip()]


# ── whisper.cpp + Vulkan (grafiki AMD, Intel, NVIDIA) ────────────────────
# Program budowany z oficjalnych źródeł whisper.cpp przez workflow w repo (.github/workflows/whisper-vulkan.yml),
# publikowany jako wydanie; suma SHA-256 chroni przed podmianą pliku.
# Test 2026-11-10 na wzorcu (ręczna transkrypcja): natywnie (cały plik, VAD whisper.cpp, słownik jako prompt
# doklejany do okien) q8 beam 2 – WER 24,1%, CER 12,2%, 65 s / 5,8 min na Iris Xe; OpenVINO 22,6% / 12,6% / 59 s;
# procesor beam 2 – 24,4% / 13,4% / 184 s. Przez nasze porcje 28 s whisper.cpp wypadał wyraźnie gorzej
# (27–29%), dlatego dostaje cały plik.
WCPP_TAG = "whisper-vulkan-v1.9.5"
WCPP_URL = f"https://github.com/Vorphy/Spisane.pl/releases/download/{WCPP_TAG}/whisper-vulkan-win-x64.zip"
WCPP_SHA256 = "43bd7e0c2026765911f4e283c45edaf7f2f6286d6cc9b7d417a4baf72732a43b"
WCPP_DIR = Path.home() / ".cache" / "whisper-cpp" / WCPP_TAG
VULKAN_BEAM = 2
DEVICE_LABELS = {"igpu": "grafika Intel (OpenVINO)", "vulkan": "grafika (Vulkan)", "cpu": "procesor",
                 "cuda": "NVIDIA (CUDA)"}


def detect_gpus():
    """Nazwy kart graficznych (bez wirtualnych i zdalnych), np. ['Intel(R) Iris(R) Xe Graphics']."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]
    names, i = [], 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not ctypes.windll.user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        i += 1
        n = d.DeviceString.strip()
        if n and n not in names and not re.search(r"microsoft|remote|virtual|displaylink|parsec|meta", n, re.I):
            names.append(n)
    return names


def vulkan_available():
    return os.name == "nt" and (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vulkan-1.dll").exists()


def auto_device():
    """Najlepsze urządzenie dla tego komputera: grafika Intel -> OpenVINO, inna grafika -> Vulkan, brak -> procesor."""
    import importlib.util
    gpus = detect_gpus()
    if any("intel" in g.lower() for g in gpus) and importlib.util.find_spec("openvino_genai"):
        return "igpu"
    if gpus and vulkan_available():
        return "vulkan"
    return "cpu"


def _download(url, dest, log, what):
    import urllib.request
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            done += len(b)
            if total and int(done * 10 / total) != shown:
                shown = int(done * 10 / total)
                log(f"Pobieram {what}: {done * 100 // total}%")
    tmp.replace(dest)


def _wcpp_binary(log):
    exe = WCPP_DIR / "whisper-cli.exe"
    if exe.exists():
        return exe
    import hashlib
    import zipfile
    WCPP_DIR.mkdir(parents=True, exist_ok=True)
    z = WCPP_DIR / "whisper.zip"
    _download(WCPP_URL, z, log, "program whisper.cpp (Vulkan, ok. 30 MB)")
    if hashlib.sha256(z.read_bytes()).hexdigest() != WCPP_SHA256:
        z.unlink(missing_ok=True)
        raise RuntimeError("pobrany program whisper.cpp ma złą sumę kontrolną – nie uruchamiam go")
    with zipfile.ZipFile(z) as zf:
        zf.extractall(WCPP_DIR)
    z.unlink(missing_ok=True)
    return exe


def _wcpp_model(name, log):
    """Model GGML z ggerganov/whisper.cpp (kwantyzacja q8_0, gdy jest – tak testowane; inaczej pełny)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError
    for fn in (f"ggml-{name}-q8_0.bin", f"ggml-{name}.bin"):
        try:
            try:
                return hf_hub_download("ggerganov/whisper.cpp", fn, local_files_only=True)
            except Exception:
                log(f"Pobieram model {fn} (jednorazowo)…")
                return hf_hub_download("ggerganov/whisper.cpp", fn)
        except EntryNotFoundError:
            continue
    raise RuntimeError(f"brak modelu {name} dla whisper.cpp")


def _wcpp_vad(log):
    from huggingface_hub import hf_hub_download
    try:
        return hf_hub_download("ggml-org/whisper-vad", "ggml-silero-v5.1.2.bin", local_files_only=True)
    except Exception:
        log("Pobieram model wykrywania mowy (1 MB)…")
        return hf_hub_download("ggml-org/whisper-vad", "ggml-silero-v5.1.2.bin")


def _write_wav(path, audio, sr=16000):
    import wave
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


class VulkanWhisper:
    """whisper.cpp na grafice przez Vulkan. Interfejs jak WhisperModel.transcribe(): generator segmentów + info.
    Cały plik idzie do whisper-cli (jego VAD i kontekst między oknami – tak wypadł najlepiej); segmenty czytamy
    na bieżąco z wyjścia programu, więc postęp, zatrzymanie i wznawianie działają jak w pozostałych silnikach."""

    SR = 16000
    _LINE = re.compile(r"\s*\[(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)\]\s?(.*)")

    def __init__(self, model_name, log=print, threads=4):
        self.log = log
        self.threads = max(1, min(int(threads), 8))
        self.exe = _wcpp_binary(log)
        self.model = _wcpp_model(model_name, log)
        self.vad = _wcpp_vad(log)
        self.gpu = self._selftest()

    def _popen(self, args, stdout, stderr):
        import subprocess
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
        p = subprocess.Popen([str(self.exe), "-m", str(self.model), "-t", str(self.threads), *args],
                             stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, creationflags=flags)
        no_power_throttling(getattr(p, "_handle", None))
        return p

    def _selftest(self):
        """Krótki przebieg na 1 s ciszy: czy program działa i czy Vulkan widzi kartę (inaczej liczyłby na CPU)."""
        import subprocess
        import tempfile
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "cisza.wav"
            _write_wav(wav, np.zeros(self.SR, np.float32))
            p = self._popen(["-f", str(wav), "-l", "pl"], subprocess.DEVNULL, subprocess.PIPE)
            _, err = p.communicate(timeout=300)
        err = err.decode("utf-8", "replace")
        m = re.search(r"ggml_vulkan: 0 = ([^|\r\n]+)", err)
        if p.returncode != 0:
            raise RuntimeError(f"whisper.cpp nie działa (kod {p.returncode})")
        if not m or "using Vulkan" not in err:
            raise RuntimeError("Vulkan nie widzi karty graficznej (stary sterownik?)")
        return m.group(1).strip()

    def transcribe(self, audio, language="pl", vad_filter=True, initial_prompt=None, beam_size=VULKAN_BEAM,
                   hotwords=None, **_ignored):
        from types import SimpleNamespace
        from faster_whisper import decode_audio
        if isinstance(audio, str):
            audio = decode_audio(audio, sampling_rate=self.SR)
        info = SimpleNamespace(duration=len(audio) / self.SR)
        prompt = " ".join(x for x in ((hotwords or "").strip(), (initial_prompt or "").strip()) if x)
        return self._segments(audio, language, vad_filter, prompt, int(beam_size)), info

    def _segments(self, audio, language, vad_filter, prompt, beam):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp(prefix="notatnik-wcpp-")
        wav = Path(d) / "audio.wav"
        _write_wav(wav, audio)
        args = ["-f", str(wav), "-l", language, "-bs", str(beam), "-np"]
        if vad_filter:
            args += ["--vad", "-vm", str(self.vad)]
        if prompt:
            args += ["--prompt", prompt, "--carry-initial-prompt"]
        p = self._popen(args, subprocess.PIPE, subprocess.DEVNULL)
        try:
            for raw in p.stdout:
                m = self._LINE.match(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if not m or not m.group(7).strip():
                    continue
                h1, m1, s1, h2, m2, s2, text = m.groups()
                yield Segment(int(h1) * 3600 + int(m1) * 60 + float(s1),
                              int(h2) * 3600 + int(m2) * 60 + float(s2), " " + text.strip())
            if p.wait() != 0:
                raise RuntimeError(f"whisper.cpp zakończył się błędem (kod {p.returncode})")
        finally:
            if p.poll() is None:            # zatrzymanie w trakcie (Zatrzymaj / zamknięcie)
                p.kill()
                p.wait()
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ── NVIDIA (CUDA) ────────────────────────────────────────────────────────
CUDA_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]   # wymagane przez CTranslate2 4.x


def cuda_devices():
    """Liczba kart NVIDIA widocznych dla CTranslate2 (0 = brak karty albo sterownika)."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def add_cuda_dll_dirs():
    """Biblioteki cuBLAS/cuDNN z pakietów pip (nvidia-*-cu12) leżą w site-packages/nvidia/*/bin –
    Windows ich sam nie znajdzie, więc dopisujemy te foldery do ścieżki wyszukiwania DLL."""
    if sys.platform != "win32":
        return []
    import site
    found = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        root = Path(sp) / "nvidia"
        if root.is_dir():
            found += [d for d in root.glob("*/bin") if d.is_dir()]
    for d in found:
        try:
            os.add_dll_directory(str(d))
        except OSError:
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(map(str, found)) + os.pathsep + os.environ.get("PATH", "")
    return found


# ── Ładowanie modelu ─────────────────────────────────────────────────────
class LoadedModel:
    def __init__(self, model, device, opts):
        self.model = model
        self.device = device        # faktyczne urządzenie (po ewentualnym przejściu igpu -> cpu)
        self.opts = opts


def load_model(opts, log=print):
    """Ładuje model wg opcji; przypina proces do rdzeni. Gdy grafika (Intel / Vulkan / NVIDIA) zawiedzie,
    przechodzi na CPU. device="auto" – wybór wg sprzętu (auto_device)."""
    device = opts["device"]
    if device == "auto":
        gpus = detect_gpus()
        device = auto_device()
        log(f"Automatycznie: {DEVICE_LABELS[device]} (karty: {', '.join(gpus) or 'brak'})")
    if device in ("cpu", "igpu"):
        log(f"Rdzenie: {apply_cores(opts['cores'])}")
    model = None
    if device == "vulkan":
        try:
            model = VulkanWhisper(opts["model"], log=log)
            log(f"Urządzenie: grafika {model.gpu} (Vulkan, whisper.cpp, beam {VULKAN_BEAM})")
        except Exception as ex:
            log(f"[Ostrzeżenie] Grafika przez Vulkan niedostępna: {ex}")
            log(f"Przełączam na CPU ({opts['threads']} wątków).")
            log(f"Rdzenie: {apply_cores(opts['cores'])}")
            device = "cpu"
    if device == "igpu":
        try:
            model = IGPUWhisper(opts["model"], log=log)
            log("Urządzenie: igpu (Intel Iris Xe, OpenVINO int8, beam 1)")
        except Exception as ex:
            log(f"[Ostrzeżenie] Grafika Iris Xe (OpenVINO) niedostępna: {ex}")
            log(f"Przełączam na CPU ({opts['threads']} wątków).")
            device = "cpu"
    from faster_whisper import WhisperModel
    if device == "cuda":
        add_cuda_dll_dirs()
        if not cuda_devices():
            log("[Ostrzeżenie] Nie wykryto karty NVIDIA (albo sterownika) – przełączam na CPU.")
            device = "cpu"
        else:
            try:
                model = WhisperModel(opts["model"], device="cuda", compute_type=opts["compute_type"])
                _cuda_selftest(model)
                log(f"Urządzenie: cuda (NVIDIA, {opts['compute_type']})")
            except Exception as ex:
                model = None
                log(f"[Ostrzeżenie] NVIDIA (CUDA) niedostępna: {ex}")
                log("Przełączam na CPU. Brakujące biblioteki: pip install " + " ".join(CUDA_PACKAGES))
                device = "cpu"
    if model is None:
        if opts["device"] == "cuda":            # przejście z NVIDIA na procesor
            log(f"Rdzenie: {apply_cores(opts['cores'])}")
        compute = opts["compute_type"] if opts["device"] != "cuda" else "int8"
        model = WhisperModel(opts["model"], device="cpu", compute_type=compute,
                             cpu_threads=int(opts["threads"]))
        log(f"Urządzenie: cpu ({compute}, {opts['threads']} wątków)")
    if not isinstance(model, (PortionWhisper, VulkanWhisper)):
        model = FasterWhisperPortions(model, log=log)     # te same porcje ≤ 28 s co na grafice
    return LoadedModel(model, device, opts)


def _cuda_selftest(model):
    """Brak cuDNN/cuBLAS wychodzi dopiero przy pierwszej transkrypcji – sprawdzamy od razu na 1 s ciszy."""
    import numpy as np
    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="pl", beam_size=1, vad_filter=False)
    list(segs)


# ── Zapis wyników ────────────────────────────────────────────────────────
def _ts_srt(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    return f"{h:02}:{m:02}:{s:02},{int((t - int(t)) * 1000):03}"


def _ts_vtt(t):
    return _ts_srt(t).replace(",", ".")


def output_paths(audio_path, fmt):
    base = Path(audio_path).with_suffix("")
    exts = {"txt": [".txt"], "srt": [".srt"], "vtt": [".vtt"], "all": [".txt", ".srt", ".vtt"]}[fmt]
    return [base.with_suffix(e) for e in exts]


def _txt_line(s, timestamps):
    return f"[{s.start:.1f}s] {s.text.strip()}\n" if timestamps else s.text.strip() + "\n"


def write_outputs(paths, segments, timestamps):
    """Ostateczny, spójny zapis plików z pełnej listy segmentów."""
    for p in paths:
        with open(p, "w", encoding="utf-8") as f:
            if p.suffix == ".txt":
                for s in segments:
                    f.write(_txt_line(s, timestamps))
            elif p.suffix == ".srt":
                for i, s in enumerate(segments, 1):
                    f.write(f"{i}\n{_ts_srt(s.start)} --> {_ts_srt(s.end)}\n{s.text.strip()}\n\n")
            else:
                f.write("WEBVTT\n\n")
                for s in segments:
                    f.write(f"{_ts_vtt(s.start)} --> {_ts_vtt(s.end)}\n{s.text.strip()}\n\n")


# ── Transkrypcja jednego pliku ───────────────────────────────────────────
def transcribe_file(lm, audio_path, opts, on_segment=None, on_progress=None, should_stop=None, log=print):
    """Transkrybuje plik; wyniki zapisuje obok niego (txt/srt/vtt wg opts['format']).
    Przerwany (should_stop() == True) zapisuje stan do wznowienia.
    Wznawia automatycznie, jeśli obok leży .transcribe_state.

    Zwraca dict: status ("done" | "stopped" | "empty"), outputs, duration, segments, elapsed."""
    from faster_whisper import decode_audio

    on_segment = on_segment or (lambda s: None)
    on_progress = on_progress or (lambda f: None)
    should_stop = should_stop or (lambda: False)
    audio_path = Path(audio_path)
    file_start = time.time()
    paths = output_paths(audio_path, opts["format"])
    timestamps = opts["timestamps"]

    # ── wznowienie ──
    state = load_state(audio_path)
    start_at = 0.0
    segs = []
    prompt = None
    limit_time = opts.get("limit_time") or None
    if state:
        start_at = float(state["last_checkpoint_seconds"])
        segs = [Segment(d["start"], d["end"], d["text"]) for d in state["segments"]]
        if state.get("limit_time"):
            limit_time = state["limit_time"]
        if segs:
            prompt = " ".join(s.text for s in segs[-5:])
        log(f"[WZNÓW] {len(segs)} segmentów, wznawiam od {fmt_elapsed(start_at)} ({start_at:.1f}s)")

    # ── przycinanie ──
    limit = None
    if limit_time:
        limit = parse_time_to_seconds(limit_time)
        if limit:
            log(f"Przycinam transkrypcję do {fmt_elapsed(limit)} ({limit:.1f}s)")
        else:
            log(f"[Ostrzeżenie] Niepoprawny limit czasu: '{limit_time}'. Transkrypcja bez limitu.")

    kwargs = {
        "language": opts["language"],
        "beam_size": {"igpu": 1, "vulkan": VULKAN_BEAM}.get(lm.device, int(opts["beam_size"])),
        "vad_filter": bool(opts["vad"]),
        # Bez przenoszenia kontekstu między oknami 30 s – zapobiega zapętlaniu zdań
        "condition_on_previous_text": False,
    }
    vocab = (opts.get("vocab") or "").strip() if opts.get("vocab_on", True) else ""
    if vocab:
        kwargs["hotwords"] = vocab
        log(f"Słownik nazw: {vocab_tokens(vocab)} tokenów")
    if start_at > 0:
        if limit and start_at >= limit:
            log("Wznowienie jest już za limitem czasu – transkrypcja kompletna.")
            write_outputs(paths, segs, timestamps)
            state_path(audio_path).unlink(missing_ok=True)
            return {"status": "done", "outputs": [str(p) for p in paths], "duration": limit,
                    "segments": len(segs), "elapsed": 0.0}
        if prompt:
            kwargs["initial_prompt"] = prompt   # działa tylko na pierwsze okno po wznowieniu

    # Wznowienie / przycięcie: wycinamy audio sami (clip_timestamps w faster-whisper ignoruje VAD),
    # timestampy segmentów przesuwamy o time_offset.
    time_offset = 0.0
    if start_at > 0 or limit:
        sr = 16000
        audio = decode_audio(str(audio_path), sampling_rate=sr)
        total = len(audio) / sr
        clip_end = min(limit, total) if limit else total
        time_offset = start_at
        audio_input = audio[int(start_at * sr):int(clip_end * sr)]
        duration = clip_end
    else:
        audio_input = str(audio_path)
        duration = None

    segments, info = lm.model.transcribe(audio_input, **kwargs)
    if duration is None:
        duration = info.duration

    interrupted = False
    last_ok = start_at
    # Zapis na bieżąco (dopisywanie), na końcu nadpisujemy całość spójną listą
    mode = "a" if start_at > 0 else "w"
    live = {p.suffix: open(p, mode, encoding="utf-8") for p in paths}
    if ".vtt" in live and start_at == 0:
        live[".vtt"].write("WEBVTT\n\n")
    try:
        for seg in segments:
            if time_offset:
                seg.start += time_offset
                seg.end += time_offset
            if limit and seg.start > limit:
                break
            if limit and seg.end > limit:
                seg.end = limit
            segs.append(seg)
            last_ok = seg.end
            if ".txt" in live:
                live[".txt"].write(_txt_line(seg, timestamps))
            if ".srt" in live:
                live[".srt"].write(f"{len(segs)}\n{_ts_srt(seg.start)} --> {_ts_srt(seg.end)}\n{seg.text.strip()}\n\n")
            if ".vtt" in live:
                live[".vtt"].write(f"{_ts_vtt(seg.start)} --> {_ts_vtt(seg.end)}\n{seg.text.strip()}\n\n")
            for f in live.values():
                f.flush()
            on_segment(seg)
            if duration:
                on_progress(min(1.0, seg.end / duration))
            if should_stop():
                interrupted = True
                break
    finally:
        for f in live.values():
            f.close()

    if segs:
        write_outputs(paths, segs, timestamps)
    if interrupted:
        save_state(audio_path, last_ok, segs, limit_time)
        log(f"Przerwano – zapisano stan do wznowienia ({fmt_elapsed(last_ok)}).")
    else:
        state_path(audio_path).unlink(missing_ok=True)
        on_progress(1.0)

    return {
        "status": "stopped" if interrupted else ("done" if segs else "empty"),
        "outputs": [str(p) for p in paths] if segs else [],
        "duration": duration,
        "segments": len(segs),
        "elapsed": time.time() - file_start,
    }


def scan_folder(folder, exclude=()):
    """Jak tryb -B w fw-dd.py: pliki audio bez .txt obok albo z przerwanym stanem."""
    folder = Path(folder)
    exclude = {Path(e).resolve() for e in exclude}
    return [f for f in sorted(folder.iterdir())
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            and f.parent.name != "przetworzone"
            and f.resolve() not in exclude
            and (not f.with_suffix(".txt").exists() or f.with_suffix(".transcribe_state").exists())]


def watch_candidates(folder, exclude=()):
    """Jak tryb -W w fw-dd.py: wszystkie pliki audio w folderze (gotowe trafiają do 'przetworzone')."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    exclude = {Path(e).resolve() for e in exclude}
    return [f for f in sorted(folder.iterdir())
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and f.resolve() not in exclude]
