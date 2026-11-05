"""
deps.py – sprawdzenie pakietów przy starcie (np. po przeniesieniu na nowy komputer).

Używa wyłącznie biblioteki standardowej (tkinter), bo uruchamia się ZANIM
zaimportujemy customtkinter, numpy itd. Pyta o zgodę i instaluje brakujące
pakiety przez pip, pokazując okienko postępu.

Grupy:
  - "app"         – bez nich aplikacja nie wystartuje (pytamy przy każdym starcie),
  - "transkrypcja" – potrzebne tylko do zakładki Transkrypcja (fw-dd.py);
                     odmowę zapamiętujemy w config.json, żeby nie męczyć co start.
"""

import importlib
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "notatnik.log"

# (moduł do importu, pakiet pip, opis, grupa)
PACKAGES = [
    ("customtkinter",  "customtkinter",  "okno aplikacji",                      "app"),
    ("numpy",          "numpy",          "obróbka dźwięku",                     "app"),
    ("pyaudiowpatch",  "PyAudioWPatch",  "nagrywanie mikrofonu i głośników",    "app"),
    ("av",             "av<19",          "zapis i konwersja do Opus",           "app"),
    ("faster_whisper", "faster-whisper", "transkrypcja (silnik Whisper)",       "transkrypcja"),
    ("openvino_genai", "openvino-genai", "transkrypcja na grafice Intel (szybciej)", "transkrypcja"),
]


def _av_too_new():
    """faster-whisper 1.2.1 nie działa z av 19+ (ten sam warunek co w fw-dd.py)."""
    try:
        return int(importlib.metadata.version("av").split(".")[0]) >= 19
    except Exception:
        return False


def _missing():
    out = [p for p in PACKAGES if importlib.util.find_spec(p[0]) is None]
    if _av_too_new() and not any(p[0] == "av" for p in out):
        out.append(("av", "av<19", "zgodna wersja av dla transkrypcji", "transkrypcja"))
    return out


def _load_cfg():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_decline(names):
    cfg = _load_cfg()
    cfg["deps_declined"] = sorted(set(cfg.get("deps_declined", [])) | set(names))
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _python_console():
    exe = Path(sys.executable)
    alt = exe.with_name("python.exe")
    return str(alt if exe.name.lower() == "pythonw.exe" and alt.exists() else exe)


def _pip_install(root, specs):
    """Instaluje w tle, pokazując okienko z postępem. Zwraca True, gdy pip zakończył się sukcesem."""
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(root)
    win.title("Notatnik spotkań – instalacja")
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: None)   # nie przerywamy pip w połowie
    ttk.Label(win, text="Instaluję pakiety:\n" + ", ".join(specs), justify="left").pack(padx=16, pady=(14, 6), anchor="w")
    bar = ttk.Progressbar(win, mode="indeterminate", length=420)
    bar.pack(padx=16, pady=4)
    bar.start(12)
    last = tk.StringVar(value="Uruchamiam pip…")
    ttk.Label(win, textvariable=last, width=70, foreground="gray").pack(padx=16, pady=(4, 14), anchor="w")
    win.update_idletasks()
    win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}+{(win.winfo_screenheight() - win.winfo_height()) // 3}")

    result = {"code": None}
    cmd = [_python_console(), "-m", "pip", "install", "--disable-pip-version-check"] + specs

    def work():
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] pip install {' '.join(specs)}\n")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in proc.stdout:
                    log.write(line)
                    line = line.strip()
                    if line:
                        root.after(0, last.set, line[:90])
                result["code"] = proc.wait()
        except Exception as e:
            result["code"] = -1
            root.after(0, last.set, str(e)[:90])
        root.after(0, win.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.wait_window(win)
    importlib.invalidate_caches()
    return result["code"] == 0


def ensure():
    """Wywołać na samym początku notatnik.pyw. Kończy program, jeśli brakuje pakietów wymaganych."""
    missing = _missing()
    declined = set(_load_cfg().get("deps_declined", []))
    ask = [p for p in missing if p[3] == "app" or p[1] not in declined]
    if not ask:
        return

    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        lines = "\n".join(f"  • {pip}  – {desc}" for _, pip, desc, _ in ask)
        need_app = any(p[3] == "app" for p in ask)
        msg = ("Na tym komputerze brakuje pakietów Pythona potrzebnych aplikacji:\n\n" + lines +
               "\n\nPobrać i zainstalować je teraz?"
               + ("\nPakiety do transkrypcji to kilkaset MB." if any(p[3] == "transkrypcja" for p in ask) else "")
               + ("\n\nBez pakietów z pierwszej grupy aplikacja nie wystartuje." if need_app else
                  "\n\n„Nie” = nagrywanie działa, transkrypcja nie (nie zapytam ponownie;\n"
                  "żeby zapytać znów, usuń „deps_declined” z config.json)."))
        if not messagebox.askyesno("Notatnik spotkań – brakujące pakiety", msg):
            if need_app:
                sys.exit(1)
            _remember_decline(p[1] for p in ask)
            return

        ok = _pip_install(root, [p[1] for p in ask])
        still = [p for p in _missing() if p in ask]
        if not ok or still:
            cmd = f'"{_python_console()}" -m pip install ' + " ".join(f'"{p[1]}"' for p in (still or ask))
            messagebox.showerror("Notatnik spotkań – instalacja nieudana",
                                 f"Nie udało się zainstalować pakietów.\nSzczegóły: {LOG_PATH}\n\n"
                                 f"Możesz spróbować ręcznie w terminalu:\n{cmd}")
            if any(p[3] == "app" for p in (still or ask)):
                sys.exit(1)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _cuda_missing():
    """Biblioteki CUDA dla CTranslate2 – tylko gdy w komputerze jest karta NVIDIA."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() == 0:
            return []
    except Exception:
        return []
    out = []
    for mod, pip, desc in (("nvidia.cublas", "nvidia-cublas-cu12", "CUDA cuBLAS dla karty NVIDIA"),
                           ("nvidia.cudnn", "nvidia-cudnn-cu12==9.*", "CUDA cuDNN dla karty NVIDIA")):
        try:
            if importlib.util.find_spec(mod) is None:
                out.append((mod, pip, desc, "transkrypcja"))
        except ModuleNotFoundError:
            out.append((mod, pip, desc, "transkrypcja"))
    return out


def ensure_transcription(root, device):
    """Przed transkrypcją: brakujące pakiety Whispera (i CUDA dla NVIDIA) -> zapytaj i doinstaluj,
    także po wcześniejszej odmowie. Zwraca False, gdy transkrypcja nie może ruszyć."""
    from tkinter import messagebox
    missing = [p for p in _missing() if p[3] == "transkrypcja" and (device == "igpu" or p[0] != "openvino_genai")]
    if device == "cuda" and importlib.util.find_spec("faster_whisper") is not None:
        missing += _cuda_missing()
    if not missing:
        return True
    lines = "\n".join(f"  • {pip}  – {desc}" for _, pip, desc, _ in missing)
    if not messagebox.askyesno("Notatnik spotkań – pakiety do transkrypcji",
                               "Do transkrypcji brakuje pakietów:\n\n" + lines +
                               "\n\nPobrać i zainstalować je teraz? (kilkaset MB)", parent=root):
        return importlib.util.find_spec("faster_whisper") is not None
    ok = _pip_install(root, [p[1] for p in missing])
    if not ok:
        messagebox.showerror("Notatnik spotkań", f"Instalacja nieudana. Szczegóły: {LOG_PATH}", parent=root)
    return importlib.util.find_spec("faster_whisper") is not None
