"""
Notatnik spotkań – nagrywanie rozmów (głośniki + mikrofon) do Opus,
konwersja starych nagrań do Opus i kolejka transkrypcji Whisperem
(wbudowana: engine.py + worker.py – dawniej zewnętrzny skrypt fw-dd.py).

Uruchomienie: pythonw notatnik.pyw   (albo skrót „Notatnik spotkań”)
"""

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import deps          # noqa: E402  – brakujące pakiety (nowy komputer): zapytaj i doinstaluj
deps.ensure()

# Per-Monitor v2: Windows skaluje pasek tytułu i ramki osobno dla każdego monitora (w trybie, który
# włącza CustomTkinter, pasek ma zawsze rozmiar monitora głównego – na monitorze 100% był za duży).
# Musi być ustawione przed utworzeniem pierwszego okna; późniejsze wywołanie w CustomTkinter nic już nie zmienia.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

import customtkinter as ctk  # noqa: E402
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker  # noqa: E402


# ── Skalowanie między monitorami (np. laptop 150% + monitory 100%) ─────────────────────────
# CustomTkinter co 100 ms sprawdza, na którym monitorze jest okno, i je przeskalowuje. Domyślnie:
# - robi to już w trakcie przeciągania (okno „pływa” duże/małe i miga półprzezroczystością),
# - liczy monitor dla całego okna, więc na granicy monitorów potrafi skakać tam i z powrotem,
# - nowe okno dostaje skalę monitora, na którym Windows je najpierw postawi, a potem skacze.
# Poprawki: skala według monitora pod lewą częścią paska tytułu (zmienia się od razu przy przeciąganiu,
# bez skakania); po przeskalowaniu okno wsuwane w całości na monitor;
# niewidoczne jeszcze okno dostaje skalę monitora okna-rodzica albo zapamiętanej pozycji.
_u32 = ctypes.windll.user32
_u32.GetParent.restype = wintypes.HWND
_u32.GetParent.argtypes = [wintypes.HWND]
_u32.MonitorFromPoint.restype = wintypes.HMONITOR
_u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
_ROOT_POS = None          # (x, y) – zapamiętana pozycja okna aplikacji, zanim się pokaże


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _frame_hwnd(window):
    h = wintypes.HWND(window.winfo_id())
    return _u32.GetParent(h) or h


def _monitor_at(x, y, default=2):          # 2 = najbliższy, 0 = żaden (None poza ekranami)
    return _u32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), default)


def _monitor_scale(hmon):
    dx, dy = wintypes.UINT(), wintypes.UINT()
    ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
    return (dx.value + dy.value) / 192


def _work_area(hmon):
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    _u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def _window_rect(window):
    r = wintypes.RECT()
    _u32.GetWindowRect(_frame_hwnd(window), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _center_monitor(window):
    left, top, right, bottom = _window_rect(window)
    return _monitor_at((left + right) / 2, (top + bottom) / 2)


def _anchor_monitor(window):
    """Monitor pod lewą częścią paska tytułu (tam, gdzie zwykle chwyta się okno). Zmiana rozmiaru nie
    przesuwa lewego górnego rogu, więc ten punkt nie „ucieka” na drugi monitor – skala zmienia się od razu
    przy przeciąganiu, jak w zwykłych programach Windows, i nie skacze tam i z powrotem."""
    left, top, right, bottom = _window_rect(window)
    return _monitor_at(left + min(120, (right - left) // 3), top + 12)


def _mouse_down():
    return bool(_u32.GetAsyncKeyState(0x01) & 0x8000)


_orig_dpi = ScalingTracker.get_window_dpi_scaling
_orig_update = ScalingTracker.update_scaling_callbacks_for_window


def _dpi_scaling(cls, window):
    try:
        if not window.winfo_viewable():
            master = getattr(window, "master", None)
            if master is None and _ROOT_POS:
                return _monitor_scale(_monitor_at(*_ROOT_POS))
            if master is not None and master.winfo_exists() and master.winfo_toplevel().winfo_viewable():
                return _monitor_scale(_center_monitor(master.winfo_toplevel()))
        return _monitor_scale(_anchor_monitor(window))
    except Exception:
        return _orig_dpi(window)


def _fit_on_monitor(window):
    """Wsuń okno w całości w obszar roboczy jego monitora (nie w trakcie przeciągania – nie walcz z myszą)."""
    try:
        if not window.winfo_exists() or not window.winfo_viewable() or _mouse_down():
            return
        left, top, right, bottom = _window_rect(window)
        L, T, R, B = _work_area(_anchor_monitor(window))
        x = int(min(max(left, L), max(L, R - (right - left))))
        y = int(min(max(top, T), max(T, B - (bottom - top))))
        if (x, y) != (left, top):
            _u32.SetWindowPos(_frame_hwnd(window), None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
    except Exception:
        pass


def _tk_nc_error(window):
    """O ile (dx, dy) Tk myli się co do grubości paska tytułu i ramek okna na jego obecnym monitorze.
    Tk liczy je ze skali monitora GŁÓWNEGO (AdjustWindowRectEx), a w trybie Per-Monitor v2 Windows rysuje
    je w skali monitora, na którym jest okno. Na monitorze głównym błąd = (0, 0)."""
    hwnd = _frame_hwnd(window)
    left, top, right, bottom = _window_rect(window)
    cr = wintypes.RECT()
    _u32.GetClientRect(hwnd, ctypes.byref(cr))
    tk_nc = wintypes.RECT(0, 0, 0, 0)
    _u32.AdjustWindowRectEx(ctypes.byref(tk_nc), _u32.GetWindowLongW(hwnd, -16), False, _u32.GetWindowLongW(hwnd, -20))
    return ((right - left - cr.right) - (tk_nc.right - tk_nc.left),
            (bottom - top - cr.bottom) - (tk_nc.bottom - tk_nc.top)), (cr.right, cr.bottom)


def _apply_min_max(window, want=None):
    """Minimum/maksimum okna w skali jego monitora, z poprawką na błąd Tk (zamiast wersji z CustomTkinter,
    która przez ten błąd rozpychała wnętrze o 6 px). Zwraca obecny rozmiar wnętrza."""
    s = ScalingTracker.get_window_scaling(window)
    (dx, dy), client = _tk_nc_error(window)
    if getattr(window, "_drag_lock", None):
        # przeciąganie trwa: rozmiar zablokowany (min = max), bo Windows po puszczeniu przycisku
        # przywraca rozmiar sprzed przeciągania – zob. _watch_release
        w0, h0 = window._drag_lock
        want = want or (round(w0 * s), round(h0 * s))
        tkinter.Wm.wm_minsize(window, want[0] + dx, want[1] + dy)
        tkinter.Wm.wm_maxsize(window, want[0] + dx, want[1] + dy)
    else:
        mn_w, mn_h = getattr(window, "_min_width", 0) or 0, getattr(window, "_min_height", 0) or 0
        tkinter.Wm.wm_minsize(window, max(1, round(mn_w * s) + dx), max(1, round(mn_h * s) + dy))
        mx_w, mx_h = getattr(window, "_max_width", None), getattr(window, "_max_height", None)
        if mx_w and mx_h and mx_w < 100000:
            tkinter.Wm.wm_maxsize(window, round(mx_w * s) + dx, round(mx_h * s) + dy)
        else:
            tkinter.Wm.wm_maxsize(window, 100000, 100000)
    return client


def _fix_client_size(window, w0=None, h0=None):
    """Ustaw wnętrze okna dokładnie na rozmiar logiczny × skala, z poprawką na błąd Tk (zob. _tk_nc_error);
    tak samo minimum/maksimum – inaczej okno na monitorze o innej skali było za duże i rosło przy przejściach."""
    try:
        if not window.winfo_exists() or not window.winfo_viewable():
            return
        w0 = w0 or window._current_width
        h0 = h0 or window._current_height
        s = ScalingTracker.get_window_scaling(window)
        want = (round(w0 * s), round(h0 * s))
        client = _apply_min_max(window, want)
        # rozmiar: Tk myli się tu inaczej niż przy min/max – ustaw, zmierz faktyczne wnętrze, popraw o różnicę
        gw, gh = want
        for _ in range(3):
            if client == want:
                break
            gw, gh = gw + want[0] - client[0], gh + want[1] - client[1]
            tkinter.Wm.wm_geometry(window, f"{gw}x{gh}")
            window.update_idletasks()
            cr = wintypes.RECT()
            _u32.GetClientRect(_frame_hwnd(window), ctypes.byref(cr))
            client = (cr.right, cr.bottom)
        window._current_width, window._current_height = w0, h0
    except Exception:
        pass


def _update_and_fit(cls, window):
    # przez chwilę po zmianie skali zdarzenia <Configure> niosą jeszcze STARY rozmiar fizyczny –
    # CustomTkinter przeliczyłby go nową skalą i „zapamiętał” złe wymiary (okno rosło po każdym przejściu)
    lock = getattr(window, "_drag_lock", None)
    w0, h0 = lock or (getattr(window, "_current_width", None), getattr(window, "_current_height", None))
    window._dims_frozen_until = time.monotonic() + 1.6
    if _mouse_down():
        # zmiana skali w trakcie przeciągania: Windows pamięta rozmiar okna sprzed przeciągania i przywraca go
        # w chwili puszczenia przycisku. CustomTkinter blokuje rozmiar tylko na 1 s – przy wolnym przeciąganiu
        # blokada znikała, a przywrócony rozmiar fizyczny zapisywał się jako nowy logiczny (okno rosło 1,5×
        # po każdym przejściu). Blokada trwa więc do puszczenia przycisku.
        window._drag_lock = (w0, h0)
        if not lock:
            window.after(50, lambda: _watch_release(window))
    _orig_update(window)

    try:
        window.after(100, lambda: _fix_client_size(window, w0, h0))
        window.after(1100, lambda: _fix_client_size(window, w0, h0))   # po zdjęciu blokady min/max (1000 ms)
        window.after(150, lambda: _fit_on_monitor(window))
        window.after(1200, lambda: _fit_on_monitor(window))
    except Exception:
        pass


def _watch_release(window):
    """Po przeciąganiu ze zmianą skali: czekaj na puszczenie przycisku, przeczekaj „powrót” rozmiaru,
    który robi Windows, i przywróć rozmiar logiczny sprzed przeciągania."""
    try:
        if not window.winfo_exists():
            return
        if _mouse_down():
            window.after(50, lambda: _watch_release(window))
            return
        w0, h0 = window._drag_lock
        window._dims_frozen_until = time.monotonic() + 1.0

        def finish():
            window._drag_lock = None
            _fix_client_size(window, w0, h0)
            _fit_on_monitor(window)
        window.after(150, finish)
        window.after(700, lambda: _fix_client_size(window, w0, h0))
    except Exception:
        window._drag_lock = None


def _patch_dimensions(cls):
    """Błędy CustomTkinter: block_update_dimensions_event() ustawiał False (blokada nie działała),
    a zdarzenia rozmiaru tuż po zmianie skali psuły zapamiętane wymiary okna."""
    orig = cls._update_dimensions_event
    orig_min_max = cls._set_scaled_min_max

    def set_scaled_min_max(self):
        try:
            if self.winfo_viewable():
                _apply_min_max(self)                  # z poprawką; w trakcie przeciągania blokada zostaje
                return
        except Exception:
            pass
        orig_min_max(self)

    def update_dimensions(self, event=None):
        try:
            if not self.winfo_viewable():       # ukryte (start, okno opcji przed pokazaniem): Tk podaje
                return                          # domyślne 200×200 – to nie jest rozmiar okna
        except Exception:
            pass
        try:
            # okno na granicy monitorów: Windows rysuje ramkę w skali monitora, na którym jest WIĘKSZOŚĆ okna
            # (może być inny niż pod paskiem tytułu) – ramka się zmienia, wnętrze kurczy/rośnie o kilka px.
            # To nie zmiana rozmiaru przez użytkownika: zostaw rozmiar logiczny, przywróć wnętrze.
            wdpi = _u32.GetDpiForWindow(_frame_hwnd(self))
            last = getattr(self, "_frame_dpi", None)
            self._frame_dpi = wdpi
            if last is not None and wdpi != last and not getattr(self, "_drag_lock", None):
                self._dims_frozen_until = max(getattr(self, "_dims_frozen_until", 0), time.monotonic() + 0.5)
                w0, h0 = self._current_width, self._current_height
                self.after(60, lambda: _fix_client_size(self, w0, h0))
                return
        except Exception:
            pass
        if getattr(self, "_drag_lock", None) or time.monotonic() < getattr(self, "_dims_frozen_until", 0):
            return
        try:
            # okno jest już na monitorze o innej skali, a CustomTkinter jeszcze nie przeskalował (do 100 ms,
            # albo dłużej – przeciąganie): rozmiar fizyczny przeliczony starą skalą byłby błędny
            if abs(_monitor_scale(_anchor_monitor(self)) - ScalingTracker.window_dpi_scaling_dict.get(self, 0)) > 0.01:
                return
        except Exception:
            pass
        orig(self, event)

    def block(self):
        self._block_update_dimensions_event = True

    cls._update_dimensions_event = update_dimensions
    cls._set_scaled_min_max = set_scaled_min_max
    cls.block_update_dimensions_event = block


_patch_dimensions(ctk.CTk)
_patch_dimensions(ctk.CTkToplevel)
ScalingTracker.get_window_dpi_scaling = classmethod(_dpi_scaling)
ScalingTracker.update_scaling_callbacks_for_window = classmethod(_update_and_fit)


def place_dialog(win, parent):
    """Okno opcji: ukryte, dopóki CustomTkinter nie skończy swoich zmian (ikona, kolor paska),
    potem pokazane raz – wyśrodkowane nad aplikacją, na jej monitorze, w skali tego monitora."""
    win.transient(parent)                   # najpierw transient – na Windows potrafi z powrotem pokazać okno
    win.withdraw()
    win.after(260, lambda: _reveal_dialog(win, parent))


def _reveal_dialog(win, parent):
    if not win.winfo_exists():
        return
    if ICON_PATH.exists():
        try:
            win.iconbitmap(str(ICON_PATH))
        except Exception:
            pass
    s = ScalingTracker.get_window_scaling(win)
    pl, pt, pr, pb = _window_rect(parent)
    L, T, R, B = _work_area(_center_monitor(parent))
    frame = round(45 * s)                               # pasek tytułu i ramki (poza rozmiarem z geometry)
    w, h = round(win._current_width * s), round(win._current_height * s) + frame
    if h > B - T - 10:                                  # wyższe niż ekran (np. laptop) – skróć
        win.geometry(f"{win._current_width}x{int((B - T - 10 - frame) / s)}")
        h = round(win._current_height * s) + frame
    x = int(min(max((pl + pr - w) / 2, L), max(L, R - w)))
    y = int(min(max(pt + 30, T), max(T, B - h)))
    win.geometry(f"+{x}+{y}")
    win.deiconify()
    win.after(30, lambda: _fix_client_size(win))
    win.lift()
    win.focus_force()
    try:
        win.grab_set()
    except Exception:
        pass

import recorder      # noqa: E402
import converter     # noqa: E402
import convproc      # noqa: E402
import transcriber   # noqa: E402
import engine        # noqa: E402

APP_NAME = "Notatnik spotkań"
CONFIG_PATH = APP_DIR / "config.json"
ICON_PATH = APP_DIR / "notatnik.ico"
LOG_PATH = APP_DIR / "notatnik.log"
LEVEL_DIR = APP_DIR / "_wyrownane"   # tymczasowe kopie z wyrównaną głośnością do transkrypcji

DEFAULTS = {
    "save_dir": str(Path.home() / "Videos"),
    "watch_dir": str(Path.home() / "Videos"),
    "tr_opts": {},               # opcje transkrypcji (uzupełniane domyślnymi z engine.DEFAULT_OPTIONS)
    "mic_name": None,
    "loop_name": None,
    "bitrate": 32000,
    "agc": True,                 # wyrównywanie na żywo – osobno mikrofon i rozmówcy
    "post_level": True,          # po nagraniu: wyrównanie głośności gotowego pliku
    "post_denoise": True,        # po nagraniu: usunięcie szumu
    "keep_raw": False,           # po obróbce zachowaj surowe nagranie w podfolderze "surowe"
    "pause_while_recording": True,
    "convert_bitrate": None,     # None = jak nagrywanie (migracja ze wspólnego "bitrate")
    "original_action": None,     # keep / trash / delete (migracja z dawnego "delete_original")
    "convert_out_dir": "",       # "" = obok oryginału
    "level_convert": True,
    "level_transcribe": True,
    "denoise_convert": True,
    "denoise_transcribe": True,
    "convert_cores": "auto",     # auto / p / e / all – też przygotowanie dźwięku do transkrypcji
    "appearance": "system",
    "always_on_top": False,      # 📌 w nagłówku
    "window_pos": None,          # [x, y] – gdzie było okno przy zamknięciu (ten sam monitor po starcie)
}

CONVERT_CORES = {"auto": "Automatycznie", "p": "P (wydajne)", "e": "E (energooszczędne)", "all": "Wszystkie"}
CORE_NAMES = {"p": "P (wydajne)", "e": "E (energooszczędne)", "all": "wszystkie"}

CONVERT_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".wma", ".ogg"}

# ── Kolory ────────────────────────────────────────────────────────────────
RED = ("#E5484D", "#E5484D")
RED_HOVER = ("#C93C40", "#F2555A")
GREEN = ("#2F9E62", "#3DD68C")
MUTED = ("#6B7280", "#9CA3AF")
CARD = ("#F3F4F6", "#23262B")
CARD_BORDER = ("#E5E7EB", "#30343A")
BG = ("#FFFFFF", "#17191C")

AMBER = ("#B45309", "#FBBF24")
PIN_ON = ("#BFDBFE", "#1E3A8A")      # 📌 włączone (zawsze na wierzchu)

STATUS_COLORS = {
    "czeka": MUTED, "w toku": ("#2563EB", "#60A5FA"), "gotowe": GREEN,
    "zatrzymano": ("#B45309", "#FBBF24"), "błąd": RED, "do wznowienia": ("#B45309", "#FBBF24"),
}


def log_exc(where):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {where}\n{traceback.format_exc()}")
            detail = getattr(sys.exc_info()[1], "detail", "")
            if detail:
                f.write(f"--- ślad z procesu ---\n{detail}\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    cfg.pop("fwdd_path", None)   # dawny zewnętrzny skrypt – transkrypcja jest teraz wbudowana
    if cfg.get("convert_bitrate") is None:          # do 2026-11-09 konwersja brała jakość nagrywania
        cfg["convert_bitrate"] = cfg["bitrate"]
    if cfg.get("original_action") not in ("keep", "trash", "delete"):
        cfg["original_action"] = "delete" if cfg.get("delete_original") else "keep"
    cfg.pop("delete_original", None)
    if cfg.get("convert_cores") not in CONVERT_CORES:
        cfg["convert_cores"] = "auto"
    cfg["tr_opts"] = {**engine.DEFAULT_OPTIONS, **{k: v for k, v in (cfg.get("tr_opts") or {}).items()
                                                   if k in engine.DEFAULT_OPTIONS}}
    return cfg


def to_recycle_bin(path):
    """Przenosi plik do Kosza (z możliwością przywrócenia). Rzuca OSError przy niepowodzeniu."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(Path(path).resolve()) + "\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted or Path(path).exists():
        raise OSError(f"Nie udało się przenieść do Kosza (kod {rc})")


def opus_target(src, out_dir=""):
    """Ścieżka wyniku konwersji: obok oryginału albo w out_dir; nie nadpisuje istniejących plików."""
    src = Path(src)
    folder = Path(out_dir) if out_dir else src.parent
    dst = folder / (src.stem + ".opus")
    n = 2
    while dst.exists() or dst.resolve() == src.resolve():
        dst = folder / f"{src.stem} ({n}).opus"
        n += 1
    return dst


def processing_plan(path, level, denoise):
    """Kroki do wykonania z pominięciem tych, które plik już przeszedł (znacznik w pliku Opus).
    Zwraca (level, denoise, notka_dla_wiersza)."""
    info = converter.read_processing(path) if Path(path).suffix.lower() in (".opus", ".ogg") else None
    if not info:
        return level, denoise, ""
    done = info["steps"]
    what = " i ".join(x for x, k in (("wyrównany", "level"), ("odszumiony", "denoise")) if k in done)
    where = {"nagrywanie": "po nagraniu", "konwersja": "przy konwersji"}.get(info["source"], "")
    try:
        when = datetime.strptime(info["when"], "%Y-%m-%d %H:%M").strftime("%d.%m, %H:%M")
    except ValueError:
        when = info["when"]
    note = "⚠ Już " + what + (f" {where}" if where else "") + (f" ({when})" if when else "")
    off = [x for x, k, on in (("wyrównanie", "level", level), ("usuwanie szumu", "denoise", denoise))
           if on and k in done]
    if off:
        note += " – " + " i ".join(off) + " wyłączone dla tego pliku"
    return level and "level" not in done, denoise and "denoise" not in done, note


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log_exc("save_config")


def fmt_hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02}:{sec % 3600 // 60:02}:{sec % 60:02}"


def fmt_size(b):
    if b < 1024 * 1024:
        return f"{b / 1024:.0f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024 / 1024:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def leveled_tmp(path):
    """Stała ścieżka kopii z wyrównaną głośnością (ta sama nazwa = wznawianie działa)."""
    import hashlib
    h = hashlib.sha1(str(Path(path).resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return LEVEL_DIR / h / (Path(path).stem + ".opus")


def reveal(path):
    path = Path(path)
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path if path.exists() else path.parent))
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════════
#  Wiersz listy plików (wspólny dla transkrypcji i konwersji)
# ══════════════════════════════════════════════════════════════════════════
class FileRow(ctk.CTkFrame):
    def __init__(self, master, path, on_remove):
        super().__init__(master, fg_color=CARD, border_color=CARD_BORDER, border_width=1, corner_radius=10)
        self.path = Path(path)
        self.status = "czeka"
        self.result = None
        self.grid_columnconfigure(0, weight=1)

        nm = self.path.name
        if len(nm) > 24:                       # wąskie okno – zostaw początek (data) i koniec (rozszerzenie)
            nm = nm[:18] + "…" + nm[-5:]
        self.name = ctk.CTkLabel(self, text=nm, anchor="w",
                                 font=ctk.CTkFont(size=11, weight="bold"))
        self.name.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=(8, 0))
        self.detail = ctk.CTkLabel(self, text=str(self.path.parent), anchor="w",
                                   text_color=MUTED, font=ctk.CTkFont(size=11))
        self.detail.grid(row=1, column=0, sticky="ew", padx=(12, 6))

        self.status_lbl = ctk.CTkLabel(self, text="czeka", text_color=MUTED,
                                       font=ctk.CTkFont(size=11, weight="bold"), width=56, anchor="e")
        self.status_lbl.grid(row=0, column=1, sticky="e", padx=4, pady=(8, 0))

        self.action = ctk.CTkButton(self, text="✕", width=28, height=28, corner_radius=8,
                                    fg_color="transparent", hover_color=CARD_BORDER,
                                    text_color=MUTED, command=lambda: on_remove(self))
        self.action.grid(row=0, column=2, rowspan=2, padx=(0, 8), pady=6)

        self.note = ctk.CTkLabel(self, text="", anchor="w", text_color=AMBER, font=ctk.CTkFont(size=11))

        self.bar = ctk.CTkProgressBar(self, height=6, corner_radius=3)
        self.bar.set(0)
        self.bar.grid(row=3, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 10))

    def set_note(self, text):
        """Informacja o pliku (np. że był już obrobiony) – bursztynowa, pod ścieżką."""
        self.note.configure(text=text)
        if text:
            self.note.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(12, 6))
        else:
            self.note.grid_remove()

    def set_status(self, status, detail=None):
        self.status = status
        self.status_lbl.configure(text=status, text_color=STATUS_COLORS.get(status, MUTED))
        if detail is not None:
            self.detail.configure(text=detail)
        if status == "gotowe":
            self.bar.set(1)
            self.bar.configure(progress_color=GREEN)

    def set_progress(self, f):
        self.bar.set(max(0.0, min(1.0, f)))

    def set_action(self, text, command, width=28):
        self.action.configure(text=text, command=command, width=width)


# ══════════════════════════════════════════════════════════════════════════
#  Lista plików z przyciskami (bazowa dla dwóch zakładek)
# ══════════════════════════════════════════════════════════════════════════
class FileQueue(ctk.CTkFrame):
    def __init__(self, master, app, empty_text):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.rows = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        bar = self.toolbar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ctk.CTkButton(bar, text="＋ Pliki", width=62, height=26, command=self.add_files).pack(side="left")
        ctk.CTkButton(bar, text="＋ Folder", width=70, height=26, command=self.add_folder).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Wyczyść", width=60, height=26, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"),
                      hover_color=CARD, command=self.clear_done).pack(side="left")

        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=1, column=0, sticky="nsew")
        self.list.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(self.list, text=empty_text, text_color=MUTED, justify="center", wraplength=250)
        self.empty.grid(row=0, column=0, pady=40)

    # do nadpisania
    def filetypes(self):
        return [("Wszystkie pliki", "*.*")]

    def scan(self, folder):
        return []

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self, title="Wybierz pliki",
                                            initialdir=self.app.cfg["save_dir"], filetypes=self.filetypes())
        self.add_paths(paths)

    def add_folder(self):
        d = filedialog.askdirectory(parent=self, title="Wybierz folder", initialdir=self.app.cfg["save_dir"])
        if not d:
            return
        found = self.scan(d)
        if not found:
            messagebox.showinfo(APP_NAME, "W tym folderze nie ma plików do przetworzenia.", parent=self)
            return
        self.add_paths(found)

    def add_paths(self, paths):
        known = {r.path.resolve() for r in self.rows}
        for p in paths:
            p = Path(p)
            if p.resolve() in known or not p.is_file():
                continue
            row = FileRow(self.list, p, self.remove_row)
            self.rows.append(row)
            self.on_added(row)
        self.relayout()

    def on_added(self, row):
        pass

    def remove_row(self, row):
        if row.status == "w toku":
            return
        self.rows.remove(row)
        row.destroy()
        self.relayout()

    def clear_done(self):
        for r in [r for r in self.rows if r.status == "gotowe"]:
            self.rows.remove(r)
            r.destroy()
        self.relayout()

    def relayout(self):
        if self.rows:
            self.empty.grid_remove()
        else:
            self.empty.grid()
        for i, r in enumerate(self.rows):
            r.grid(row=i + 1, column=0, sticky="ew", pady=4, padx=(0, 4))
        self.start_hint()

    START_VERB = ""            # nazwa przycisku startu, np. "Konwertuj"

    def start_hint(self):
        """„czeka” nie znaczy, że ruszy samo – podpowiedz, że trzeba kliknąć start."""
        live = getattr(self, "live", None)
        if live is None or getattr(self, "running", False):
            return
        hint = f"Pliki czekają w kolejce – kliknij „▶ {self.START_VERB}”, żeby zacząć."
        if self.next_waiting():
            live.configure(text=hint)
        elif live.cget("text") == hint:
            live.configure(text="")

    def next_waiting(self):
        return next((r for r in self.rows if r.status in ("czeka", "do wznowienia")), None)


# ══════════════════════════════════════════════════════════════════════════
#  Zakładka: Transkrypcja
# ══════════════════════════════════════════════════════════════════════════
class TranscribeTab(FileQueue):
    START_VERB = "Transkrybuj"
    WATCH_EVERY = 3.0          # co ile sekund sprawdzać monitorowany folder (jak fw-dd.py -W)

    def __init__(self, master, app):
        super().__init__(master, app, "Dodaj nagrania albo cały folder.\n"
                                      "Z folderu trafią tu pliki bez transkrypcji (.txt).\n"
                                      "Albo włącz monitor folderu – nowe nagrania dodadzą się same.")
        self.running = False
        self.worker = None                 # transcriber.Worker (osobny proces z modelem)
        self.worker_ready = False          # model załadowany
        self.pending = None                # (row, audio) czekające na załadowanie modelu
        self.current = None                # (row, audio) w trakcie transkrypcji
        self.preparing = None              # wiersz, dla którego trwa wyrównywanie głośności
        self.prep_cancel = threading.Event()
        self.watching = False
        self.ignored = set()               # pliki usunięte z listy w trakcie monitorowania
        self._last_watch = 0.0

        ctk.CTkButton(self.toolbar, text="⚙", width=30, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"), hover_color=CARD,
                      command=lambda: TranscribeOptions(self.app, self)).pack(side="right")
        self.summary = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=11),
                                    cursor="hand2", wraplength=270, justify="left")
        self.summary.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.summary.bind("<Button-1>", lambda e: TranscribeOptions(self.app, self))
        self.update_summary()

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.live = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED, wraplength=280, justify="left",
                                 font=ctk.CTkFont(size=11, slant="italic"))
        self.live.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.watch_sw = ctk.CTkSwitch(foot, text="", command=self._watch_changed)
        self.watch_sw.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._watch_label()
        self.btn = ctk.CTkButton(foot, text="▶  Transkrybuj", width=112, height=30,
                                 font=ctk.CTkFont(size=13, weight="bold"), command=self.toggle)
        self.btn.grid(row=1, column=1, sticky="e", pady=(4, 0))

    # ── opcje ──
    @property
    def opts(self):
        return self.app.cfg["tr_opts"]

    def update_summary(self):
        o = self.opts
        dev = {"igpu": "grafika Intel", "cpu": f"CPU ({o['threads']} wątków, rdzenie {o['cores']})",
               "cuda": "NVIDIA"}[o["device"]]
        parts = [o["model"], o["language"], dev, o["format"] + ("" if o["timestamps"] else " bez czasu")]
        if o["device"] != "igpu":
            parts.append(f"beam {o['beam_size']}")
        if not o["vad"]:
            parts.append("bez VAD")
        c = self.app.cfg
        fx = [x for x, on in (("wyrównanie", c["level_transcribe"]), ("bez szumu", c["denoise_transcribe"])) if on]
        if fx:
            parts.append(" + ".join(fx))
        self.summary.configure(text="⚙  " + "  ·  ".join(parts))

    def _watch_label(self):
        # wąskie okno – bez nazwy folderu (jest w Opcjach i w linii nad przełącznikiem, gdy monitor działa)
        self.watch_sw.configure(text="Monitoruj folder")   # co robi – widać w linii nad nim

    def _watch_changed(self):
        self.watching = bool(self.watch_sw.get())
        if self.watching:
            d = Path(self.app.cfg["watch_dir"])
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Nie mogę utworzyć folderu:\n{d}\n\n{e}", parent=self)
                self.watch_sw.deselect()
                self.watching = False
                return
            self.ignored.clear()
            self._last_watch = 0.0
            if not self.running:
                self._start_running()
        elif self.running and not self.busy and not self.next_waiting(peek=True):
            self.running = False
            self._idle()
            self.live.configure(text="Monitor folderu wyłączony.")

    def filetypes(self):
        return [("Nagrania", " ".join("*" + e for e in sorted(transcriber.AUDIO_EXTENSIONS))),
                ("Wszystkie pliki", "*.*")]

    def scan(self, folder):
        rec = self.app.rec_tab.current_path()
        return transcriber.scan_folder(folder, exclude=[rec] if rec else [])

    def on_added(self, row):
        if not hasattr(row, "limit"):
            row.limit = ctk.CTkEntry(row, width=78, height=22, font=ctk.CTkFont(size=11),
                                     placeholder_text="✂ przytnij")
            row.limit.grid(row=1, column=1, sticky="e", padx=4)
            row.limit.bind("<FocusOut>", lambda e, r=row: self._check_limit(r))
            row.limit.bind("<Return>", lambda e, r=row: self._check_limit(r))
        row.set_note(processing_plan(row.path, self.app.cfg["level_transcribe"],
                                     self.app.cfg["denoise_transcribe"])[2])
        state = None
        for st in (row.path.with_suffix(".transcribe_state"), leveled_tmp(row.path).with_suffix(".transcribe_state")):
            if st.exists():
                state = st
        if state:
            row.set_status("do wznowienia")
            try:                                   # przerwany plik pamięta swój limit
                lim = json.loads(state.read_text(encoding="utf-8")).get("limit_time") or ""
            except (OSError, ValueError):
                lim = ""
            row.limit.delete(0, "end")
            if lim:
                row.limit.insert(0, lim)
            row.limit.configure(state="disabled")

    def _check_limit(self, row):
        """Zwraca limit z pola wiersza ('' = całe nagranie) albo None, gdy format jest zły."""
        v = row.limit.get().strip()
        ok = not v or engine.parse_time_to_seconds(v)
        row.limit.configure(border_color=CARD_BORDER if ok else RED)
        return v if ok else None

    def remove_row(self, row):
        if row.status != "w toku" and self.watching:
            self.ignored.add(row.path.resolve())
        super().remove_row(row)

    def next_waiting(self, peek=False):
        """Pierwszy plik gotowy do transkrypcji. Pliki z monitora czekają, aż skończy się ich zapis."""
        for r in self.rows:
            if (r.status == "błąd" and r.detail.cget("text").startswith("Przytnij do")
                    and self._check_limit(r) is not None):
                r.set_status("czeka", str(r.path.parent))
            if r.status not in ("czeka", "do wznowienia"):
                continue
            w = getattr(r, "watcher", None)
            if w is not None and not peek:
                if not w.ready():
                    r.detail.configure(text=f"{r.path.parent}   ·   {w.note}")
                    continue
                r.watcher = None
                r.detail.configure(text=str(r.path.parent))
            return r
        return None

    def _scan_watch(self):
        if not self.watching or time.time() - self._last_watch < self.WATCH_EVERY:
            return
        self._last_watch = time.time()
        rec = self.app.rec_tab.current_path()
        known = {r.path.resolve() for r in self.rows} | self.ignored
        new = [f for f in transcriber.watch_candidates(self.app.cfg["watch_dir"], exclude=[rec] if rec else [])
               if f.resolve() not in known]
        if not new:
            return
        before = set(self.rows)
        self.add_paths(new)
        for r in self.rows:
            if r not in before and r.status == "czeka":   # przerwane (do wznowienia) – bez czekania
                r.watcher = engine.WriteWatcher(r.path)

    # ── start / stop ──
    def toggle(self):
        if self.running:
            self.running = False
            if self.watching:
                self.watch_sw.deselect()
                self.watching = False
            if self.preparing:
                self.prep_cancel.set()
                self.btn.configure(text="Zatrzymuję…", state="disabled")
            elif self.pending:
                row, _ = self.pending          # model jeszcze się ładuje – po prostu zamykamy proces
                self.pending = None
                self._kill_worker()
                self._reset_row(row)
                self.live.configure(text="Zatrzymano.")
                self._idle()
            elif self.current:
                self.btn.configure(text="Zatrzymuję…", state="disabled")
                self.live.configure(text="Zapisuję stan, żeby można było później wznowić…")
                self.worker.stop()
            else:
                self._idle()
                self.live.configure(text="")
        else:
            if not self.next_waiting(peek=True):
                messagebox.showinfo(APP_NAME, "Brak plików oczekujących na transkrypcję.\n"
                                              "Możesz też włączyć monitor folderu.", parent=self)
                return
            self._start_running()

    def _start_running(self):
        if not deps.ensure_transcription(self.app, self.opts["device"]):
            messagebox.showerror(APP_NAME, "Bez pakietu faster-whisper transkrypcja nie zadziała.", parent=self)
            if self.watching:
                self.watch_sw.deselect()
                self.watching = False
            return
        self.running = True
        self.btn.configure(text="■  Zatrzymaj", fg_color=RED, hover_color=RED_HOVER)
        self.tick()

    def _idle(self):
        self.btn.configure(text="▶  Transkrybuj", state="normal",
                           fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                           hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])

    def _reset_row(self, row):
        row.set_progress(0)
        row.action.configure(state="normal")
        row.set_status("czeka")
        row.limit.configure(state="normal")
        self.on_added(row)

    def tick(self):
        """Wywoływane cyklicznie – monitor folderu i start kolejnego pliku, gdy można."""
        self._scan_watch()
        if not self.running or self.busy:
            return
        row = self.next_waiting()
        if not row:
            self._release_worker()
            if self.watching:
                waiting = any(getattr(r, "watcher", None) for r in self.rows)
                self.live.configure(text="⏳  Nowy plik – czekam, aż skończy się zapis…" if waiting else
                                    f"👁  Czekam na nowe nagrania w {self.app.cfg['watch_dir']}…")
                return
            self.running = False
            self._idle()
            self.live.configure(text="Wszystkie pliki przetworzone ✓")
            return
        if self.app.rec_tab.is_recording() and self.app.cfg["pause_while_recording"]:
            self.live.configure(text="⏸  Czekam na zakończenie nagrywania…")
            return
        if row.path == self.app.rec_tab.current_path():
            self.live.configure(text="⏸  Ten plik jest właśnie nagrywany – czekam…")
            return
        if row.path in self.app.rec_tab.processing:
            self.live.configure(text="⏸  Nagranie jest jeszcze przetwarzane po zakończeniu – czekam…")
            return
        limit = self._check_limit(row)
        if limit is None:
            row.set_status("błąd", "Przytnij do: wpisz minuty (np. 45) albo GG:MM:SS (np. 01:15:00)")
            self.after(10, self.tick)
            return
        row.limit_value = limit
        row.limit.configure(state="disabled")
        row.set_status("w toku")
        row.action.configure(state="disabled")
        level, denoise, note = processing_plan(row.path, self.app.cfg["level_transcribe"],
                                               self.app.cfg["denoise_transcribe"])
        row.set_note(note)              # plik mógł zostać obrobiony po dodaniu do kolejki
        tmp = leveled_tmp(row.path)
        if tmp.exists() and tmp.with_suffix(".transcribe_state").exists():
            self._launch(row, tmp)            # wznowienie – kopia już jest
            return
        if not (level or denoise) or row.path.with_suffix(".transcribe_state").exists():
            self._launch(row, row.path)       # bez obróbki albo wznowienie oryginału
            return
        # wyrównanie głośności w tle, potem transkrypcja kopii
        self.preparing = row
        self.prep_cancel.clear()
        what = " i ".join(x for x, on in (("Wyrównuję głośność", level),
                                          ("usuwam szum" if level else "Usuwam szum", denoise)) if on)
        self.live.configure(text=what + "…")
        ui = self.app.ui

        def on_cores(kind, desc):
            ui(lambda: self.preparing is row and self.live.configure(
                text=f"{what}…   ·   rdzenie {CORE_NAMES.get(kind, kind)}"))

        def work():
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                convproc.convert(row.path, tmp, bitrate=48000, level=level, denoise=denoise,
                                 cores=self.app.cfg["convert_cores"], recording=self.app.rec_tab.is_recording,
                                 on_cores=on_cores, progress=lambda f: ui(lambda: row.set_progress(f)),
                                 cancel=self.prep_cancel.is_set)
                ui(lambda: self._prepared(row, tmp, None))
            except InterruptedError:
                ui(lambda: self._prepared(row, tmp, "stop"))
            except Exception as e:
                log_exc(f"level {row.path}")
                ui(lambda m=str(e): self._prepared(row, tmp, m))
        threading.Thread(target=work, daemon=True).start()

    def _prepared(self, row, tmp, err):
        self.preparing = None
        if err == "stop":
            self._reset_row(row)
            self.live.configure(text="Zatrzymano.")
            self._idle()
            return
        if err:
            row.set_status("błąd", f"Przygotowanie dźwięku: {err}")
            row.action.configure(state="normal")
            self.tick()
            return
        row.set_progress(0)
        if not self.running:          # ktoś kliknął Zatrzymaj w ostatniej chwili
            self._reset_row(row)
            self._idle()
            return
        self._launch(row, tmp)

    # ── proces z modelem ──
    def _launch(self, row, audio):
        opts = dict(self.opts)
        if self.worker and self.worker.alive and self.worker.key == transcriber.model_key(opts):
            if self.worker_ready:
                self._send_job(row, audio)
            else:
                self.pending = (row, audio)
            return
        self._kill_worker()
        self.live.configure(text="Ładowanie modelu…")
        ui = self.app.ui
        w = transcriber.Worker(lambda ev: ui(lambda: self._on_event(w, ev)))
        self.worker, self.worker_ready, self.pending = w, False, (row, audio)
        try:
            w.start(opts)
        except Exception as e:
            log_exc("worker start")
            self.worker = self.pending = None
            row.set_status("błąd", str(e))
            row.action.configure(state="normal")

    def _send_job(self, row, audio):
        self.current = (row, audio)
        self.live.configure(text="Transkrybuję…")
        self.worker.job(audio, dict(self.opts, limit_time=getattr(row, "limit_value", "")))

    def _release_worker(self):
        if self.worker:
            self.worker.quit()
        self.worker = None
        self.worker_ready = False

    def _kill_worker(self):
        if self.worker:
            self.worker.kill()
        self.worker = None
        self.worker_ready = False

    def _on_event(self, w, ev):
        if w is not self.worker:
            return                      # zdarzenie ze starego, już zamkniętego procesu
        kind = ev["ev"]
        if kind == "loaded":
            self.worker_ready = True
            if ev["device"] != self.opts["device"]:
                self.live.configure(text=f"Grafika niedostępna – transkrypcja na {ev['device'].upper()}")
            if self.pending:
                row, audio = self.pending
                self.pending = None
                self._send_job(row, audio)
        elif kind == "stderr":
            if self.pending:            # pobieranie / ładowanie modelu
                self.live.configure(text="Ładowanie modelu (pierwszy raz pobiera ~1–3 GB)…  " + ev["line"][-60:])
        elif kind == "log":
            if ev["msg"].startswith(("[VAD]", "[WZNÓW]", "Przycinam", "Grafika", "[Ostrzeżenie]")):
                self.live.configure(text=ev["msg"][:120])
        elif kind == "progress" and self.current:
            self.current[0].set_progress(ev["f"])
        elif kind == "segment" and self.current:
            self.live.configure(text="„" + ev["text"][:110] + "”")
        elif kind == "done" and self.current:
            row, audio = self.current
            self.current = None
            self._done(row, audio, ev)
        elif kind in ("error", "exit"):
            target = self.current or self.pending
            self.current = self.pending = None
            log_tail = list(w.log[-60:])
            self._kill_worker()
            if target:
                msg = ev.get("msg") or f"proces transkrypcji zakończył się (kod {ev.get('code')})"
                self._fail(target[0], msg, log_tail)

    def _fail(self, row, msg, log_tail):
        row.limit.configure(state="normal")
        row.set_status("błąd", f"{msg[:90]}   ·   szczegóły w notatnik.log")
        row.set_progress(0)
        row.action.configure(state="normal")
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Transkrypcja nieudana: {row.path}\n{msg}\n")
                f.write("\n".join(log_tail) + "\n")
        except OSError:
            pass
        if not self.running:
            self._idle()
        self.tick()

    def _move_results(self, row, audio, outputs):
        """Jak move_to_processed z fw-dd.py: wyniki i oryginał do podfolderu 'przetworzone'
        (jeśli plik już tam leży – zostaje, bez przetworzone/przetworzone). Zwraca nowe ścieżki wyników."""
        import shutil
        orig = row.path
        dest_dir = orig.parent if orig.parent.name == "przetworzone" else orig.parent / "przetworzone"
        dest_dir.mkdir(exist_ok=True)
        moved = []
        for o in map(Path, outputs):
            if o.exists():
                dst = dest_dir / (orig.stem + o.suffix)
                if o != dst:
                    dst.unlink(missing_ok=True)
                    shutil.move(str(o), str(dst))
                moved.append(dst)
        for st in (orig.with_suffix(".transcribe_state"), Path(audio).with_suffix(".transcribe_state")):
            st.unlink(missing_ok=True)
        if orig.parent != dest_dir and orig.exists():
            dst = dest_dir / orig.name
            dst.unlink(missing_ok=True)
            shutil.move(str(orig), str(dst))
        if Path(audio) != orig:
            shutil.rmtree(leveled_tmp(orig).parent, ignore_errors=True)
        return moved

    def _done(self, row, audio, res):
        row.action.configure(state="normal")
        if res["status"] == "stopped":
            row.set_status("do wznowienia", "Zatrzymano – stan zapisany, kliknij Transkrybuj, by wznowić")
            self.live.configure(text="Zatrzymano.")
            self._release_worker()
            if not self.running:
                self._idle()
            return
        try:
            moved = self._move_results(row, audio, res["outputs"])
        except Exception as e:
            log_exc("move results")
            moved = [Path(o) for o in res["outputs"]]
            self.live.configure(text=f"Nie udało się przenieść do 'przetworzone': {e}")
        stats = (f"{fmt_hms(res['duration'] or 0)} nagrania  ·  {res['segments']} segm.  ·  "
                 f"{fmt_hms(res['elapsed'])} pracy")
        if not moved:
            row.set_status("gotowe", f"Brak mowy do zapisania   ·   {stats}")
        else:
            txt = next((p for p in moved if p.suffix == ".txt"), moved[0])
            row.result = txt
            row.set_status("gotowe", f"→ {txt}   ·   {stats}")
            row.set_action("Otwórz", lambda p=txt: os.startfile(str(p)), width=70)
        if not self.running:
            self._idle()
            self._release_worker()
        self.tick()

    @property
    def busy(self):
        return self.current is not None or self.pending is not None or self.preparing is not None

    def shutdown(self, timeout=60):
        """Przy zamykaniu aplikacji: przerwij z zapisem stanu i poczekaj, aż proces skończy."""
        self.running = False
        self.prep_cancel.set()
        w = self.worker
        if not w or not w.alive:
            return
        if not self.current:
            w.kill()
            return
        proc = w.proc
        w.stop()
        w.quit()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
#  Okno: Opcje transkrypcji (dawne pytania trybu interaktywnego fw-dd.py)
# ══════════════════════════════════════════════════════════════════════════
class TranscribeOptions(ctk.CTkToplevel):
    LANGS = ["pl", "en", "de", "fr", "es", "it", "uk", "ru", "cs"]
    DEVICES = {"igpu": "Grafika Intel", "cpu": "Procesor", "cuda": "NVIDIA"}
    CORES = {"e": "E (energooszczędne)", "p": "P (wydajne)", "all": "wszystkie", "n": "pierwsze N"}

    def __init__(self, app, tab):
        super().__init__(app)
        self.app, self.tab = app, tab
        self.title("Opcje transkrypcji")
        self.geometry("660x700")
        self.minsize(560, 420)
        place_dialog(self, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        o = dict(engine.DEFAULT_OPTIONS, **app.cfg["tr_opts"])
        body = self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        body.grid_columnconfigure(1, weight=1)
        self.r = 0

        # ── model, język ──
        self.model = ctk.CTkOptionMenu(body, width=200, values=engine.MODELS, command=lambda _: self._sync())
        self.model.set(o["model"])
        self.model_hint = self._place("Model Whispera", self.model, " ")

        self.lang = ctk.CTkComboBox(body, width=120, values=self.LANGS)
        self.lang.set(o["language"])
        self._place("Język", self.lang, "kod języka nagrania, np. pl, en, de, fr")

        # ── urządzenie ──
        self.device = ctk.CTkSegmentedButton(body, values=list(self.DEVICES.values()), command=lambda _: self._sync())
        self.device.set(self.DEVICES[o["device"]])
        self._place("Urządzenie", self.device,
                    "Grafika Intel Iris Xe: ok. 6–8× szybciej, niż trwa nagranie (beam 1). W teście z ręczną "
                    "transkrypcją jakość równa procesorowi z beam 2. Bez grafiki Intel samo przejdzie na procesor.\n"
                    "Procesor: ok. 2,5× wolniej niż grafika; beam 2+ daje tu niewiele.\n"
                    "NVIDIA (CUDA): dla komputerów z kartą GeForce/RTX – zwykle najszybsza opcja; brakujące "
                    "biblioteki CUDA aplikacja zaproponuje doinstalować. Bez karty NVIDIA samo przejdzie na procesor.")

        # wartości dla procesora/NVIDIA – przy grafice Intel pola pokazują stałe int8 / 1
        self._cpu_vals = {"compute": o["compute_type"], "beam": str(o["beam_size"])}
        self._igpu_shown = False
        self.compute = ctk.CTkOptionMenu(body, width=160, values=engine.COMPUTE_TYPES)
        self.compute.set(o["compute_type"])
        self.compute_hint = self._place("Typ obliczeń", self.compute, " ")

        self.beam = ctk.CTkEntry(body, width=70)
        self.beam.insert(0, str(o["beam_size"]))
        self.beam_hint = self._place("Beam size", self.beam, " ")

        self.threads = ctk.CTkEntry(body, width=70)
        self.threads.insert(0, str(o["threads"]))
        self._place("Wątki CPU", self.threads, "dla rdzeni P najlepiej 4")

        n_p, n_e = engine.core_summary()
        cf = ctk.CTkFrame(body, fg_color="transparent")
        self.cores = ctk.CTkSegmentedButton(cf, values=list(self.CORES.values()), command=lambda _: self._sync())
        self.cores.pack(side="left")
        self.cores_n = ctk.CTkEntry(cf, width=56, placeholder_text="N")
        self.cores_n.pack(side="left", padx=6)
        cur = str(o["cores"])
        self.cores.set(self.CORES.get(cur, self.CORES["n"]))
        if cur not in self.CORES:
            self.cores_n.insert(0, cur)
        self._place("Rdzenie", cf,
                    f"Ten komputer: {n_p} CPU typu P i {n_e} typu E. Dotyczy tylko procesu transkrypcji "
                    "(okno i nagrywanie działają normalnie). E zostawia rdzenie P dla innych programów, P = szybciej.")

        # ── wynik ──
        self.fmt = ctk.CTkSegmentedButton(body, values=engine.FORMATS)
        self.fmt.set(o["format"])
        self._place("Format", self.fmt, "txt – tekst · srt / vtt – napisy · all – wszystkie naraz")

        self.ts = ctk.CTkSwitch(body, text="znaczniki czasu w pliku txt, np. [12.3s]")
        if o["timestamps"]:
            self.ts.select()
        self._place("Timestampy", self.ts)

        self.vad = ctk.CTkSwitch(body, text="filtr usuwania ciszy (VAD) – zalecany")
        if o["vad"]:
            self.vad.select()
        self._place("VAD", self.vad)

        # ── przygotowanie dźwięku i nagrywanie ──
        self.level_sw = ctk.CTkSwitch(body, text="wyrównaj głośność")
        if app.cfg["level_transcribe"]:
            self.level_sw.select()
        self._place("Przed transkrypcją", self.level_sw)
        self.nr_sw = ctk.CTkSwitch(body, text="usuń szum tła")
        if app.cfg["denoise_transcribe"]:
            self.nr_sw.select()
        self._place("", self.nr_sw,
                    "na kopii pliku – oryginał zostaje bez zmian. Pliki już obrobione (np. nagrania z aplikacji) "
                    "aplikacja rozpozna i nie obrobi drugi raz")
        self.pause_sw = ctk.CTkSwitch(body, text="nie zaczynaj nowego pliku, gdy trwa nagrywanie")
        if app.cfg["pause_while_recording"]:
            self.pause_sw.select()
        self._place("Podczas nagrywania", self.pause_sw,
                    "rozpoczęty plik dokończy się normalnie; kolejne poczekają na koniec nagrania")

        # ── monitor ──
        self.watch_dir = ctk.StringVar(value=app.cfg["watch_dir"])
        wf = ctk.CTkFrame(body, fg_color="transparent")
        ctk.CTkEntry(wf, textvariable=self.watch_dir, width=330).pack(side="left")
        ctk.CTkButton(wf, text="…", width=36, command=self._pick_watch).pack(side="left", padx=6)
        self._place("Monitor folderu", wf,
                    "folder obserwowany przełącznikiem „Monitoruj folder”; gotowe pliki trafiają do jego "
                    "podfolderu „przetworzone”")

        ctk.CTkLabel(body, text="Pierwsza transkrypcja pobiera model (large-v3-turbo: ok. 0,8–1,5 GB) i kompiluje go "
                                "pod grafikę – to trwa kilka minut. Kolejne starty zajmują kilka sekund.",
                     text_color=MUTED, wraplength=560, justify="left", font=ctk.CTkFont(size=11)
                     ).grid(row=self.r, column=0, columnspan=2, sticky="w", padx=10, pady=(16, 4))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=1, column=0, sticky="ew", padx=18, pady=12)
        ctk.CTkButton(btns, text="Domyślne", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self._defaults).pack(side="left")
        self.err = ctk.CTkLabel(btns, text="", text_color=RED)
        self.err.pack(side="left", padx=10)
        ctk.CTkButton(btns, text="Zapisz", width=90, command=self._save).pack(side="right")
        ctk.CTkButton(btns, text="Anuluj", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self.destroy).pack(side="right", padx=6)
        self._sync()

    def _place(self, text, widget, hint=None):
        ctk.CTkLabel(self.body, text=text, anchor="w").grid(row=self.r, column=0, sticky="nw", padx=(10, 12), pady=(12, 0))
        widget.grid(row=self.r, column=1, sticky="w", pady=(12, 0))
        self.r += 1
        if hint is None:
            return None
        h = ctk.CTkLabel(self.body, text=hint, anchor="w", justify="left", text_color=MUTED,
                         wraplength=440, font=ctk.CTkFont(size=11))
        h.grid(row=self.r, column=1, sticky="w")
        self.r += 1
        return h

    def _dev(self):
        return {v: k for k, v in self.DEVICES.items()}[self.device.get()]

    def _set_entry(self, entry, value):
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, value)

    def _sync(self):
        dev = self._dev()
        self.model_hint.configure(text=engine.MODEL_INFO.get(self.model.get(), ""))
        igpu = dev == "igpu"
        if igpu and not self._igpu_shown:          # zapamiętaj ustawienia procesora, pokaż stałe wartości
            self._cpu_vals = {"compute": self.compute.get(), "beam": self.beam.get()}
            self.compute.set("int8")
            self._set_entry(self.beam, "1")
        elif not igpu and self._igpu_shown:         # wróć do ustawień procesora
            self.compute.set(self._cpu_vals["compute"])
            self._set_entry(self.beam, self._cpu_vals["beam"])
        self._igpu_shown = igpu
        for w in (self.compute, self.beam):
            w.configure(state="disabled" if igpu else "normal")
        if igpu:
            self.compute_hint.configure(text="Grafika Intel: model jest zawsze int8 (tak go przygotowano pod OpenVINO).")
            self.beam_hint.configure(text="Grafika Intel obsługuje tylko beam 1 – OpenVINO nie ma beam search na GPU. "
                                          "Fragmenty pominięte przez model nadrabia weryfikacja VAD. "
                                          "Wyższy beam (np. 2 dla ważnych nagrań): wybierz Procesor.")
        else:
            self.compute_hint.configure(text="int8 – najszybszy pod CPU; float16 – zalecany dla NVIDIA")
            self.beam_hint.configure(text="wyższy = dokładniejszy, ale wolniejszy (domyślnie 2)")
        if dev == "cuda" and not engine.cuda_devices():
            self.err.configure(text="Na tym komputerze nie ma karty NVIDIA – transkrypcja pójdzie na procesor")
        else:
            self.err.configure(text="")
        self.threads.configure(state="normal" if dev == "cpu" else "disabled")
        self.cores_n.configure(state="normal" if self.cores.get() == self.CORES["n"] else "disabled")

    def _pick_watch(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.watch_dir.get())
        if d:
            self.watch_dir.set(str(Path(d)))

    def _defaults(self):
        self.app.cfg["tr_opts"] = dict(engine.DEFAULT_OPTIONS)
        for k in ("level_transcribe", "denoise_transcribe", "pause_while_recording"):
            self.app.cfg[k] = DEFAULTS[k]
        save_config(self.app.cfg)
        self.tab.update_summary()
        self.destroy()
        TranscribeOptions(self.app, self.tab)

    def _save(self):
        igpu = self._dev() == "igpu"
        beam_txt = self._cpu_vals["beam"] if igpu else self.beam.get()
        compute = self._cpu_vals["compute"] if igpu else self.compute.get()
        try:
            beam, threads = int(beam_txt), int(self.threads.get())
            if beam < 1 or threads < 1:
                raise ValueError
        except ValueError:
            self.err.configure(text="Beam size i wątki: liczba całkowita ≥ 1")
            return
        cores = {v: k for k, v in self.CORES.items()}[self.cores.get()]
        if cores == "n":
            try:
                cores = engine.parse_cores(self.cores_n.get())
                int(cores)
            except ValueError:
                self.err.configure(text="Rdzenie: podaj liczbę N ≥ 1")
                return
        self.app.cfg["tr_opts"] = {
            "model": self.model.get(), "language": self.lang.get().strip().lower() or "pl",
            "device": self._dev(), "compute_type": compute, "threads": threads, "cores": cores,
            "format": self.fmt.get(), "beam_size": beam, "timestamps": bool(self.ts.get()),
            "vad": bool(self.vad.get()), "limit_time": "",
        }
        self.app.cfg["level_transcribe"] = bool(self.level_sw.get())
        self.app.cfg["denoise_transcribe"] = bool(self.nr_sw.get())
        self.app.cfg["pause_while_recording"] = bool(self.pause_sw.get())
        if self.watch_dir.get() != self.app.cfg["watch_dir"]:
            self.app.cfg["watch_dir"] = self.watch_dir.get()
            self.tab.ignored.clear()
            self.tab._watch_label()
        save_config(self.app.cfg)
        self.tab.update_summary()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════
#  Okno opcji – wspólna podstawa (Nagrywanie, Konwersja)
# ══════════════════════════════════════════════════════════════════════════
QUALITIES = {24000: "24 kbps", 32000: "32 kbps", 48000: "48 kbps"}
ORIGINAL_ACTIONS = {"keep": "Zostaw", "trash": "Do Kosza", "delete": "Usuń na stałe"}


def quality_label(bitrate):
    return QUALITIES.get(bitrate, "32 kbps")


def quality_value(label):
    return {v: k for k, v in QUALITIES.items()}[label]


class OptionsDialog(ctk.CTkToplevel):
    """Okno opcji zakładki: przewijana lista „etykieta – kontrolka – podpowiedź” i przyciski na dole."""

    def __init__(self, app, title, size="620x520"):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.geometry(size)
        self.minsize(520, 360)
        place_dialog(self, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        self.body.grid_columnconfigure(1, weight=1)
        self.r = 0

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=1, column=0, sticky="ew", padx=18, pady=12)
        ctk.CTkButton(btns, text="Domyślne", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self._defaults).pack(side="left")
        self.err = ctk.CTkLabel(btns, text="", text_color=RED)
        self.err.pack(side="left", padx=10)
        ctk.CTkButton(btns, text="Zapisz", width=90, command=self._save).pack(side="right")
        ctk.CTkButton(btns, text="Anuluj", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self.destroy).pack(side="right", padx=6)

    def _place(self, text, widget, hint=None):
        ctk.CTkLabel(self.body, text=text, anchor="w").grid(row=self.r, column=0, sticky="nw",
                                                             padx=(10, 12), pady=(12, 0))
        widget.grid(row=self.r, column=1, sticky="w", pady=(12, 0))
        self.r += 1
        if hint is None:
            return None
        h = ctk.CTkLabel(self.body, text=hint, anchor="w", justify="left", text_color=MUTED,
                         wraplength=400, font=ctk.CTkFont(size=11))
        h.grid(row=self.r, column=1, sticky="w")
        self.r += 1
        return h

    def _folder_field(self, var, width=320):
        f = ctk.CTkFrame(self.body, fg_color="transparent")
        f.entry = ctk.CTkEntry(f, textvariable=var, width=width)
        f.entry.pack(side="left")
        f.button = ctk.CTkButton(f, text="…", width=36, command=lambda: self._pick_dir(var))
        f.button.pack(side="left", padx=6)
        return f

    def _pick_dir(self, var):
        d = filedialog.askdirectory(parent=self, initialdir=var.get() or self.app.cfg["save_dir"])
        if d:
            var.set(str(Path(d)))

    def _defaults(self):
        for k in self.KEYS:
            self.app.cfg[k] = DEFAULTS[k]
        self._after_defaults()
        save_config(self.app.cfg)
        self._changed()
        self.destroy()
        type(self)(self.app)

    def _after_defaults(self):
        pass

    def _changed(self):
        pass


class RecordOptions(OptionsDialog):
    KEYS = ("save_dir", "bitrate", "agc", "post_level", "post_denoise", "keep_raw")

    def __init__(self, app):
        super().__init__(app, "Opcje nagrywania", "640x640")
        cfg = app.cfg
        self.save_dir = ctk.StringVar(value=cfg["save_dir"])
        self._place("Folder nagrań", self._folder_field(self.save_dir),
                    "tu trafiają nowe nagrania (nazwa pliku = data i godzina)")

        self.quality = ctk.CTkSegmentedButton(self.body, values=list(QUALITIES.values()))
        self.quality.set(quality_label(cfg["bitrate"]))
        self._place("Jakość nagrania", self.quality,
                    "24 kbps ≈ 11 MB na godzinę · 32 kbps ≈ 14 MB · 48 kbps ≈ 22 MB.\n"
                    "32 kbps w zupełności wystarcza do mowy.")

        self.post_level = ctk.CTkSwitch(self.body, text="wyrównaj głośność", command=self._sync)
        if cfg["post_level"]:
            self.post_level.select()
        self._place("Po nagraniu", self.post_level)
        self.post_denoise = ctk.CTkSwitch(self.body, text="usuń szum tła")
        if cfg["post_denoise"]:
            self.post_denoise.select()
        self._place("", self.post_denoise,
                    "Mikrofon i rozmówcy są nagrywani osobno, a po nagraniu każdy jest obrabiany oddzielnie "
                    "(własny profil szumu i poziom) i dopiero wtedy łączony. Trwa ok. 3–5 min na godzinę "
                    "nagrania (rdzenie jak w Opcjach konwersji). Transkrypcja i konwersja nie powtórzą tych kroków.")

        self.agc = ctk.CTkSwitch(self.body, text="wyrównuj na żywo")
        if cfg["agc"]:
            self.agc.select()
        self.agc_hint = self._place("Zapasowo", self.agc, " ")
        self.keep_raw = ctk.CTkSwitch(self.body, text="zachowaj surowe nagranie w podfolderze „surowe”")
        if cfg["keep_raw"]:
            self.keep_raw.select()
        self._place("", self.keep_raw, "Domyślnie obrobiony plik zastępuje surowy.")
        self._sync()

    def _sync(self):
        post = bool(self.post_level.get())
        self.agc.configure(state="disabled" if post else "normal")
        self.agc_hint.configure(text="Nieużywane, bo głośność wyrówna się po nagraniu (dokładniej)." if post else
                                "Lekkie wyrównywanie w trakcie, osobno mikrofon i rozmówcy – "
                                "gdy nie chcesz wyrównania po nagraniu.")

        ctk.CTkLabel(self.body, text="Zmiany obowiązują od następnego nagrania. Mikrofon i głośniki "
                                     "wybierasz bezpośrednio w zakładce Nagrywanie.",
                     text_color=MUTED, wraplength=520, justify="left", font=ctk.CTkFont(size=11)
                     ).grid(row=self.r, column=0, columnspan=2, sticky="w", padx=10, pady=(18, 4))

    def _after_defaults(self):
        self.app.cfg["save_dir"] = str(Path.home() / "Videos")

    def _changed(self):
        self.app.rec_tab.update_summary()

    def _save(self):
        d = self.save_dir.get().strip()
        if not d:
            self.err.configure(text="Wybierz folder nagrań")
            return
        cfg = self.app.cfg
        cfg["save_dir"] = str(Path(d))
        cfg["bitrate"] = quality_value(self.quality.get())
        cfg["agc"] = bool(self.agc.get())
        cfg["post_level"] = bool(self.post_level.get())
        cfg["post_denoise"] = bool(self.post_denoise.get())
        cfg["keep_raw"] = bool(self.keep_raw.get())
        save_config(cfg)
        self._changed()
        self.destroy()


class ConvertOptions(OptionsDialog):
    KEYS = ("convert_bitrate", "level_convert", "denoise_convert", "convert_cores",
            "original_action", "convert_out_dir")
    OUT = {"beside": "Obok oryginału", "folder": "Wybrany folder"}
    CORE_HINTS = {
        "auto": "E podczas nagrywania, P poza nim – przełącza się w trakcie pliku.",
        "p": "Najszybciej (ok. 2× szybciej niż E), ale konkuruje z aplikacjami na pierwszym planie.",
        "e": "Wolniej, za to rdzenie P zostają wolne dla Teams i innych aplikacji.",
        "all": "Windows sam przydziela – konwersja i tak używa jednego wątku.",
    }
    ORIGINAL_HINTS = {
        "keep": "Oryginał zostaje na miejscu.",
        "trash": "Oryginał trafia do Kosza – w razie pomyłki można go przywrócić.",
        "delete": "Oryginał jest usuwany bez Kosza – nie da się go przywrócić.",
    }

    def __init__(self, app):
        super().__init__(app, "Opcje konwersji", "640x640")
        cfg = app.cfg

        self.quality = ctk.CTkSegmentedButton(self.body, values=list(QUALITIES.values()))
        self.quality.set(quality_label(cfg["convert_bitrate"]))
        self._place("Jakość pliku", self.quality,
                    "24 kbps ≈ 11 MB na godzinę · 32 kbps ≈ 14 MB · 48 kbps ≈ 22 MB.\n"
                    "Niezależna od jakości nagrywania – np. stare MP4 można mocniej skompresować.")

        self.level = ctk.CTkSwitch(self.body, text="cisi rozmówcy podgłośnieni")
        if cfg["level_convert"]:
            self.level.select()
        self._place("Wyrównanie głośności", self.level)
        self.denoise = ctk.CTkSwitch(self.body, text="filtr buczenia i bramka szumu tła")
        if cfg["denoise_convert"]:
            self.denoise.select()
        self._place("Usuwanie szumu", self.denoise,
                    "Obie opcje wydłużają konwersję (ok. 2–3×), ale wciąż trwa ona minuty, nie godziny.")

        self.cores = ctk.CTkSegmentedButton(self.body, values=list(CONVERT_CORES.values()),
                                            command=lambda _: self._sync())
        self.cores.set(CONVERT_CORES[cfg["convert_cores"]])
        self.cores_hint = self._place("Rdzenie CPU", self.cores, " ")

        self.original = ctk.CTkSegmentedButton(self.body, values=list(ORIGINAL_ACTIONS.values()),
                                               command=lambda _: self._sync())
        self.original.set(ORIGINAL_ACTIONS[cfg["original_action"]])
        self.original_hint = self._place("Oryginał po konwersji", self.original, " ")

        self.out_mode = ctk.CTkSegmentedButton(self.body, values=list(self.OUT.values()),
                                               command=lambda _: self._sync())
        self.out_mode.set(self.OUT["folder" if cfg["convert_out_dir"] else "beside"])
        self._place("Zapis wyniku", self.out_mode)
        self.out_dir = ctk.StringVar(value=cfg["convert_out_dir"])
        self.out_field = self._folder_field(self.out_dir)
        self._place("", self.out_field,
                    "Plik .opus nie nadpisuje istniejących – przy powtórzonej nazwie dostaje dopisek (2), (3)…")

        ctk.CTkLabel(self.body, text="Rdzenie CPU, wyrównanie i usuwanie szumu dotyczą też przygotowania "
                                     "dźwięku przed transkrypcją (tam włączasz je przełącznikami w zakładce).",
                     text_color=MUTED, wraplength=540, justify="left", font=ctk.CTkFont(size=11)
                     ).grid(row=self.r, column=0, columnspan=2, sticky="w", padx=10, pady=(18, 4))
        self._sync()

    def _key(self, mapping, label):
        return {v: k for k, v in mapping.items()}[label]

    def _sync(self):
        cores = self._key(CONVERT_CORES, self.cores.get())
        original = self._key(ORIGINAL_ACTIONS, self.original.get())
        self.cores_hint.configure(text=self.CORE_HINTS[cores])
        extra = ("" if original == "keep" else
                 "\nPrzed usunięciem sprawdzam, czy długość nagrania się zgadza. Z pliku wideo zostaje tylko dźwięk.")
        self.original_hint.configure(text=self.ORIGINAL_HINTS[original] + extra,
                                     text_color=RED if original == "delete" else MUTED)
        state = "normal" if self.out_mode.get() == self.OUT["folder"] else "disabled"
        self.out_field.entry.configure(state=state)
        self.out_field.button.configure(state=state)

    def _after_defaults(self):
        self.app.cfg["convert_bitrate"] = 32000
        self.app.cfg["original_action"] = "keep"

    def _changed(self):
        self.app.conv_tab.update_summary()

    def _save(self):
        out = ""
        if self.out_mode.get() == self.OUT["folder"]:
            out = self.out_dir.get().strip()
            if not out:
                self.err.configure(text="Wybierz folder na wyniki")
                return
            out = str(Path(out))
        cfg = self.app.cfg
        cfg["convert_bitrate"] = quality_value(self.quality.get())
        cfg["level_convert"] = bool(self.level.get())
        cfg["denoise_convert"] = bool(self.denoise.get())
        cfg["convert_cores"] = self._key(CONVERT_CORES, self.cores.get())
        cfg["original_action"] = self._key(ORIGINAL_ACTIONS, self.original.get())
        cfg["convert_out_dir"] = out
        save_config(cfg)
        self._changed()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════
#  Zakładka: Konwersja
# ══════════════════════════════════════════════════════════════════════════
class ConvertTab(FileQueue):
    START_VERB = "Konwertuj"

    def __init__(self, master, app):
        super().__init__(master, app, "Dodaj nagrania MP4 (np. z OBS) albo cały folder.\n"
                                      "Powstanie plik .opus – wielokrotnie mniejszy.")
        self.running = False
        self._started = 0.0
        self.worker = None
        self.cancel = threading.Event()
        self.saved_total = 0      # faktycznie odzyskane (oryginały usunięte na stałe)
        self.trashed = 0          # oryginały w Koszu – miejsce wróci po opróżnieniu Kosza
        self.potential = 0        # do odzyskania po ręcznym usunięciu oryginałów

        ctk.CTkButton(self.toolbar, text="⚙", width=30, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"), hover_color=CARD,
                      command=lambda: ConvertOptions(self.app)).pack(side="right")
        self.summary = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=11),
                                    cursor="hand2", wraplength=270, justify="left")
        self.summary.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.summary.bind("<Button-1>", lambda e: ConvertOptions(self.app))
        self.update_summary()

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.live = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED, wraplength=280, justify="left",
                                 font=ctk.CTkFont(size=11, slant="italic"))
        self.live.grid(row=0, column=0, sticky="ew")
        self.info = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED, wraplength=160, justify="left",
                                 font=ctk.CTkFont(size=11))
        self.info.grid(row=1, column=0, sticky="ew")
        self.btn = ctk.CTkButton(foot, text="▶  Konwertuj", width=112, height=30,
                                 font=ctk.CTkFont(size=13, weight="bold"), command=self.toggle)
        self.btn.grid(row=0, column=1, rowspan=2, sticky="e", pady=(6, 0))

    def update_summary(self):
        c = self.app.cfg
        parts = [quality_label(c["convert_bitrate"])]
        fx = [x for x, on in (("wyrównanie", c["level_convert"]), ("bez szumu", c["denoise_convert"])) if on]
        parts.append(" + ".join(fx) if fx else "bez obróbki")
        parts.append("rdzenie " + {"auto": "automatycznie"}.get(c["convert_cores"],
                                                               CORE_NAMES.get(c["convert_cores"], "")))
        parts.append({"keep": "oryginał zostaje", "trash": "oryginał do Kosza",
                      "delete": "oryginał usuwany na stałe"}[c["original_action"]])
        out = c["convert_out_dir"]
        parts.append(f"zapis do „{Path(out).name or out}”" if out else "zapis obok oryginału")
        self.summary.configure(text="⚙  " + "  ·  ".join(parts))

    def filetypes(self):
        return [("Nagrania wideo/audio", " ".join("*" + e for e in sorted(CONVERT_EXT))),
                ("Wszystkie pliki", "*.*")]

    def _converted(self, f):
        out = self.app.cfg["convert_out_dir"]
        return f.with_suffix(".opus").exists() or bool(out and (Path(out) / (f.stem + ".opus")).exists())

    def scan(self, folder):
        return [f for f in sorted(Path(folder).iterdir())
                if f.is_file() and f.suffix.lower() in CONVERT_EXT and not self._converted(f)]

    def on_added(self, row):
        try:
            row.detail.configure(text=f"{row.path.parent}   ·   {fmt_size(row.path.stat().st_size)}")
        except OSError:
            pass
        row.set_note(processing_plan(row.path, self.app.cfg["level_convert"], self.app.cfg["denoise_convert"])[2])

    def toggle(self):
        if self.running:
            if time.monotonic() - self._started < 1.5:     # podwójne kliknięcie „Konwertuj” – nie zatrzymuj
                return
            self.cancel.set()
            self.btn.configure(text="Zatrzymuję…", state="disabled")
            return
        if not self.next_waiting():
            messagebox.showinfo(APP_NAME, "Brak plików do konwersji.", parent=self)
            return
        out = self.app.cfg["convert_out_dir"]
        if out:
            try:
                Path(out).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Nie mogę użyć folderu na wyniki:\n{out}\n\n{e}", parent=self)
                return
        if self.app.cfg["original_action"] == "delete" and not messagebox.askyesno(
                APP_NAME, "Oryginały zostaną usunięte NA STAŁE (bez Kosza) po udanej konwersji – "
                          "sprawdzam długość nagrania.\nObraz z plików wideo przepadnie. Kontynuować?",
                parent=self):
            return
        self.running = True
        self._started = time.monotonic()
        self.cancel.clear()
        self.btn.configure(text="■  Zatrzymaj", fg_color=RED, hover_color=RED_HOVER)
        self.live.configure(text="Konwertuję…")
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _work(self):
        ui = self.app.ui
        c = dict(self.app.cfg)          # ustawienia z chwili startu
        action = c["original_action"]

        def on_cores(kind, desc):
            ui(lambda: self.running and self.live.configure(
                text=f"Konwertuję…   ·   rdzenie {CORE_NAMES.get(kind, kind)}"))

        while not self.cancel.is_set():
            row = self.next_waiting()
            if not row:
                break
            ui(lambda r=row: (r.set_status("w toku"), r.action.configure(state="disabled")))
            level, denoise, _ = processing_plan(row.path, c["level_convert"], c["denoise_convert"])
            try:
                before = row.path.stat().st_size
                dst = convproc.convert(
                    row.path, opus_target(row.path, c["convert_out_dir"]), bitrate=c["convert_bitrate"],
                    level=level, denoise=denoise,
                    cores=c["convert_cores"], recording=self.app.rec_tab.is_recording, on_cores=on_cores,
                    progress=lambda f, r=row: ui(lambda: r.set_progress(f)),
                    cancel=self.cancel.is_set)
                after = dst.stat().st_size
                note = f"{fmt_size(before)} → {fmt_size(after)}"
                if c["convert_out_dir"]:
                    note += f"   ·   w „{dst.parent.name}”"
                if action == "keep":
                    self.potential += before - after
                elif not converter.verify(row.path, dst):
                    note += "   ·   długość się nie zgadza – oryginał zostawiony!"
                elif action == "trash":
                    try:
                        to_recycle_bin(row.path)
                        self.trashed += before - after
                        note += "   ·   oryginał w Koszu"
                    except OSError:
                        log_exc(f"trash {row.path}")
                        note += "   ·   nie udało się przenieść oryginału do Kosza – zostawiony"
                else:
                    row.path.unlink()
                    self.saved_total += before - after
                    note += "   ·   oryginał usunięty"
                ui(lambda r=row, n=note, d=dst: self._row_done(r, n, d))
            except InterruptedError:
                ui(lambda r=row: (r.set_status("czeka"), r.set_progress(0), r.action.configure(state="normal")))
                break
            except Exception as e:
                log_exc(f"convert {row.path}")
                ui(lambda r=row, m=str(e): (r.set_status("błąd", m), r.action.configure(state="normal")))
        ui(self._finished)

    def _row_done(self, row, note, dst):
        row.set_status("gotowe", note)
        row.result = dst
        row.action.configure(state="normal")
        row.set_action("Pokaż", lambda p=dst: reveal(p), width=64)

    def _finished(self):
        self.running = False
        self.live.configure(text="Zatrzymano – „Konwertuj” zacznie przerwany plik od nowa."
                            if self.cancel.is_set() else "")
        self.btn.configure(text="▶  Konwertuj", state="normal",
                           fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                           hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])
        parts = []
        if self.saved_total > 0:
            parts.append(f"Odzyskane miejsce: {fmt_size(self.saved_total)}")
        if self.trashed > 0:
            parts.append(f"W Koszu: {fmt_size(self.trashed)} (zwolnisz po opróżnieniu Kosza)")
        if self.potential > 0:
            parts.append(f"Po usunięciu oryginałów odzyskasz: {fmt_size(self.potential)}")
        self.info.configure(text="   ·   ".join(parts))

    @property
    def busy(self):
        return self.running


# ══════════════════════════════════════════════════════════════════════════
#  Zakładka: Nagrywanie
# ══════════════════════════════════════════════════════════════════════════
class RecordTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.rec = None
        self.last_file = None
        self.blink = False
        self.grid_columnconfigure(0, weight=1)

        # ── Licznik ──
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, pady=(6, 0))
        self.dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=16), text_color=MUTED)
        self.dot.pack(side="left", padx=(0, 10))
        self.timer = ctk.CTkLabel(top, text="00:00:00", font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
                                  height=36)
        self.timer.pack(side="left")
        self.state_lbl = ctk.CTkLabel(self, text="Gotowy do nagrywania", text_color=MUTED, height=20,
                                      wraplength=280, font=ctk.CTkFont(size=12))
        self.state_lbl.grid(row=1, column=0)

        # ── Wskaźniki poziomu ──
        meters = ctk.CTkFrame(self, fg_color=CARD, border_color=CARD_BORDER, border_width=1, corner_radius=12)
        meters.grid(row=2, column=0, sticky="ew", padx=4, pady=(6, 0))
        meters.grid_columnconfigure(1, weight=1)
        self.devs_in, self.devs_loop, def_in, def_loop = [], [], None, None
        try:
            self.devs_in, self.devs_loop, def_in, def_loop = recorder.list_devices()
        except Exception:
            log_exc("list_devices")

        def pick(devs, saved_name, default_idx):
            names = [n for _, n in devs]
            if saved_name in names:
                return saved_name
            for i, n in devs:
                if i == default_idx:
                    return n
            return names[0] if names else "(brak urządzeń)"

        ctk.CTkLabel(meters, text="🎙", anchor="w", width=20).grid(
            row=0, column=0, sticky="w", padx=(10, 4), pady=(8, 0))
        self.mic_var = ctk.StringVar(value=pick(self.devs_in, app.cfg["mic_name"], def_in))
        self.mic_menu = ctk.CTkOptionMenu(meters, variable=self.mic_var, dynamic_resizing=False, height=26,
                                          values=[n for _, n in self.devs_in] or ["(brak urządzeń)"],
                                          command=lambda _: self._dev_changed())
        self.mic_menu.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(8, 0))
        self.mic_bar = ctk.CTkProgressBar(meters, height=6, progress_color=GREEN)
        self.mic_bar.set(0)
        self.mic_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 6))

        ctk.CTkLabel(meters, text="🔊", anchor="w", width=20).grid(
            row=2, column=0, sticky="w", padx=(10, 4))
        self.loop_var = ctk.StringVar(value=pick(self.devs_loop, app.cfg["loop_name"], def_loop))
        self.loop_menu = ctk.CTkOptionMenu(meters, variable=self.loop_var, dynamic_resizing=False, height=26,
                                           values=[n for _, n in self.devs_loop] or ["(brak urządzeń)"],
                                           command=lambda _: self._dev_changed())
        self.loop_menu.grid(row=2, column=1, sticky="ew", padx=(0, 10))
        self.loop_bar = ctk.CTkProgressBar(meters, height=6, progress_color=GREEN)
        self.loop_bar.set(0)
        self.loop_bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 2))
        self.check_btn = ctk.CTkButton(meters, text="Sprawdź poziomy (10 s)", width=140, height=22,
                                       fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                       text_color=("gray20", "gray85"), hover_color=CARD,
                                       command=self.start_monitor)
        self.check_btn.grid(row=4, column=0, columnspan=2, pady=(2, 6))

        # ── Przycisk ──
        self.btn = ctk.CTkButton(self, text="●   Nagrywaj", width=180, height=36, corner_radius=18,
                                 font=ctk.CTkFont(size=15, weight="bold"),
                                 fg_color=RED, hover_color=RED_HOVER, command=self.toggle)
        self.btn.grid(row=3, column=0, pady=(8, 2))

        # ── Info o pliku ──
        self.file_lbl = ctk.CTkLabel(self, text="", text_color=MUTED, height=18, wraplength=280,
                                     font=ctk.CTkFont(size=11))
        self.file_lbl.grid(row=4, column=0)
        links = ctk.CTkFrame(self, fg_color="transparent")
        links.grid(row=5, column=0, pady=(2, 0))
        links.grid_remove()          # pusta ramka miałaby domyślne 200 px wysokości – pokaże się po nagraniu
        self.to_tr = ctk.CTkButton(links, text="Do transkrypcji", width=110, height=26,
                                   fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                   text_color=("gray20", "gray85"), hover_color=CARD, command=self._to_transcribe)
        self.show = ctk.CTkButton(links, text="Pokaż plik", width=80, height=26,
                                  fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                  text_color=("gray20", "gray85"), hover_color=CARD,
                                  command=lambda: reveal(self.last_file))
        self._links = links

        # ── Opcje ──
        ctk.CTkButton(self, text="⚙", width=30, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"), hover_color=CARD,
                      command=lambda: RecordOptions(self.app)).place(relx=1.0, x=-6, y=6, anchor="ne")
        self.grid_rowconfigure(6, weight=1)
        self.summary = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=11),
                                    cursor="hand2", wraplength=270, justify="left")
        self.summary.grid(row=7, column=0, sticky="ew", padx=12, pady=(2, 2))
        self.summary.bind("<Button-1>", lambda e: RecordOptions(self.app))
        self.update_summary()

        # Mikrofon NIE jest otwierany w tle – tylko podczas nagrywania
        # albo na 10 s po kliknięciu "Sprawdź poziomy" (żeby nie przeszkadzać Teams itp.).
        self.monitor = None
        self._monitor_job = None

        # Obróbka gotowych nagrań (wyrównanie / szum) – osobny proces, jak konwersja.
        self.processing = set()            # ścieżki nagrań w trakcie obróbki (transkrypcja na nie czeka)
        self.post_cancel = threading.Event()
        self.post_threads = []

    def update_summary(self):
        c = self.app.cfg
        d = Path(c["save_dir"])
        post = [x for x, on in (("wyrównanie", c["post_level"]), ("bez szumu", c["post_denoise"])) if on]
        parts = [f"„{d.name or d}”", quality_label(c["bitrate"]),
                 "po nagraniu: " + (" + ".join(post) if post else "bez obróbki")]
        if not c["post_level"]:
            parts.append("wyrównywanie na żywo" if c["agc"] else "bez wyrównywania")
        self.summary.configure(text="⚙  " + "  ·  ".join(parts))

    # ── podgląd poziomów bez nagrywania ──
    def start_monitor(self):
        self.stop_monitor()
        if self.rec:
            return
        try:
            self.monitor = recorder.LevelMonitor(
                self._dev_index(self.devs_in, self.mic_var.get()),
                self._dev_index(self.devs_loop, self.loop_var.get()))
            if self.monitor.errors:
                self.state_lbl.configure(text="Nie mogę otworzyć urządzenia – wybierz inne", text_color=RED)
                log_exc("monitor: " + "; ".join(self.monitor.errors))
        except Exception:
            log_exc("monitor start")
            self.monitor = None
        if self.monitor:
            self.check_btn.configure(state="disabled", text="Sprawdzam…")
            self._monitor_job = self.after(10000, self.stop_monitor)

    def stop_monitor(self):
        if self._monitor_job:
            self.after_cancel(self._monitor_job)
            self._monitor_job = None
        if self.monitor:
            self.monitor.close()
            self.monitor = None
        self.mic_bar.set(0)
        self.loop_bar.set(0)
        self.check_btn.configure(state="normal", text="Sprawdź poziomy (10 s)")

    # ── stan ──
    def is_recording(self):
        return self.rec is not None

    def current_path(self):
        return self.rec.path if self.rec else None

    def _dev_index(self, devs, name):
        return next((i for i, n in devs if n == name), None)

    def _dev_changed(self):
        self.app.cfg["mic_name"] = self.mic_var.get()
        self.app.cfg["loop_name"] = self.loop_var.get()
        save_config(self.app.cfg)
        self.state_lbl.configure(text="Gotowy do nagrywania", text_color=MUTED)
        self.stop_monitor()

    def toggle(self):
        if self.rec:
            self.stop()
        else:
            self.start()

    def start(self):
        save_dir = Path(self.app.cfg["save_dir"])
        path = save_dir / f"{datetime.now():%Y-%m-%d %H-%M-%S}.opus"
        mic = self._dev_index(self.devs_in, self.mic_var.get())
        loop = self._dev_index(self.devs_loop, self.loop_var.get())
        self.stop_monitor()
        try:
            c = self.app.cfg
            # Wariant B: mikrofon i rozmówcy w osobnych kanałach; po nagraniu każdy obrabiany osobno
            # i miksowany do mono. Na żywo wyrównujemy tylko wtedy, gdy nie będzie wyrównania po nagraniu.
            self.rec = recorder.Recorder(path, mic, loop, bitrate=c["bitrate"],
                                         agc=c["agc"] and not c["post_level"], stereo=True)
            self.rec.start()
        except Exception as e:
            log_exc("record start")
            self.rec = None
            messagebox.showerror(APP_NAME, f"Nie udało się rozpocząć nagrywania:\n{e}", parent=self)
            return
        self.btn.configure(text="■   Zatrzymaj", fg_color=("#1F2937", "#E5E7EB"),
                           hover_color=("#111827", "#FFFFFF"), text_color=("#FFFFFF", "#111827"))
        self.mic_menu.configure(state="disabled")
        self.loop_menu.configure(state="disabled")
        self.check_btn.configure(state="disabled")
        self.state_lbl.configure(text="Nagrywanie…", text_color=RED)
        self.file_lbl.configure(text=str(path))
        self._links.grid_remove()

    def stop(self, warn=None):
        rec, self.rec = self.rec, None
        try:
            rec.stop()
        except Exception:
            log_exc("record stop")
        self.last_file = rec.path
        self.btn.configure(text="●   Nagrywaj", fg_color=RED, hover_color=RED_HOVER,
                           text_color=ctk.ThemeManager.theme["CTkButton"]["text_color"])
        self.mic_menu.configure(state="normal")
        self.loop_menu.configure(state="normal")
        self.check_btn.configure(state="normal")
        self.dot.configure(text_color=MUTED)
        self.mic_bar.set(0)
        self.loop_bar.set(0)
        err = warn or (f"Błąd zapisu: {rec.error}" if rec.error else None)
        self.state_lbl.configure(text=err or "Zapisano ✓", text_color=RED if err else GREEN)
        if rec.path.exists():
            self.file_lbl.configure(text=f"{rec.path.name}   ·   {fmt_hms(rec.elapsed)}   ·   {fmt_size(rec.size)}")
            self.to_tr.pack(side="left", padx=4)
            self.show.pack(side="left", padx=4)
            self.to_tr.configure(state="normal", text="Do transkrypcji")
            self._links.grid()
            if not rec.error:
                self._post_process(rec.path)       # zawsze: przynajmniej miks kanałów do mono
        self.app.update_title()

    # ── obróbka po nagraniu ──
    def _post_status(self, text, color=MUTED):
        if not self.rec:                     # podczas kolejnego nagrania etykieta należy do niego
            self.state_lbl.configure(text=text, text_color=color)

    def _post_process(self, path):
        c = dict(self.app.cfg)
        level, denoise, keep = c["post_level"], c["post_denoise"], c["keep_raw"]
        what = " i ".join(x for x, on in (("wyrównuję głośność", level), ("usuwam szum", denoise)) if on) \
            or "łączę ścieżki"
        tmp = path.with_name(path.stem + ".obrobka.tmp")      # bez rozszerzenia audio – monitor go nie weźmie
        self.processing.add(path)
        self._post_status(f"Zapisano ✓  Teraz {what}…")
        ui = self.app.ui

        def work():
            err = None
            try:
                convproc.convert(path, tmp, bitrate=c["bitrate"], level=level, denoise=denoise,
                                 cores=c["convert_cores"], recording=self.is_recording, source="nagrywanie",
                                 split_channels=True,
                                 progress=lambda f: ui(lambda: self._post_status(
                                     f"Zapisano ✓  Teraz {what}… {f:.0%}")),
                                 cancel=self.post_cancel.is_set)
                if not converter.verify(path, tmp):
                    raise RuntimeError("długość po obróbce się nie zgadza")
                if keep:
                    raw = opus_target(path, path.parent / "surowe")
                    raw.parent.mkdir(exist_ok=True)
                    shutil.copy2(path, raw)
                os.replace(tmp, path)          # ten sam folder – podmiana w jednym kroku
            except InterruptedError:
                err = "stop"
            except Exception as e:
                log_exc(f"obróbka nagrania {path}")
                err = str(e)
            if err:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            ui(lambda: self._post_done(path, err))

        t = threading.Thread(target=work, daemon=True)
        self.post_threads = [x for x in self.post_threads if x.is_alive()] + [t]
        t.start()

    def _post_done(self, path, err):
        self.processing.discard(path)
        if err == "stop":
            return
        if err:
            self._post_status(f"Obróbka się nie udała – zostało surowe nagranie ({err})", RED)
            return
        self._post_status("Zapisano i obrobiono ✓" if (self.app.cfg["post_level"] or self.app.cfg["post_denoise"])
                          else "Zapisano ✓", GREEN)
        if not self.rec and self.last_file == path and path.exists():
            parts = self.file_lbl.cget("text").split("   ·   ")
            if len(parts) == 3:
                self.file_lbl.configure(text=f"{parts[0]}   ·   {parts[1]}   ·   {fmt_size(path.stat().st_size)}")

    def _to_transcribe(self):
        if self.last_file and self.last_file.exists():
            self.app.tr_tab.add_paths([self.last_file])
            self.to_tr.configure(state="disabled", text="Dodano ✓")

    def tick(self):
        if not self.rec:
            if self.monitor:
                m, l = self.monitor.levels
                self.mic_bar.set(m)
                self.loop_bar.set(l)
            return
        r = self.rec
        self.timer.configure(text=fmt_hms(r.elapsed))
        m, l = r.levels
        self.mic_bar.set(m)
        self.loop_bar.set(l)
        # urządzenie odłączone / błąd zapisu
        dead = r.error or (r.mic and not r.mic.stream.is_active())
        if dead:
            self.stop(warn="Urządzenie odłączone albo błąd – nagrywanie zatrzymane, plik zapisany")

    def blink_tick(self):
        if self.rec:
            self.blink = not self.blink
            self.dot.configure(text_color=RED if self.blink else ("#F8B4B6", "#5B2224"))


# ══════════════════════════════════════════════════════════════════════════
#  Ustawienia
# ══════════════════════════════════════════════════════════════════════════
class SettingsWindow(ctk.CTkToplevel):
    """⚙ na samej górze: ustawienia całej aplikacji. Opcje nagrywania, transkrypcji
    i konwersji są w zakładkach (przycisk ⚙ Opcje)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Ustawienia aplikacji")
        self.geometry("520x220")
        self.resizable(False, False)
        place_dialog(self, app)
        self.grid_columnconfigure(1, weight=1)
        cfg = app.cfg

        ctk.CTkLabel(self, text="Wygląd", anchor="w").grid(row=0, column=0, sticky="w", padx=(18, 12), pady=(18, 8))
        self.look = ctk.CTkSegmentedButton(self, values=["Systemowy", "Jasny", "Ciemny"])
        self.look.set({"system": "Systemowy", "light": "Jasny", "dark": "Ciemny"}.get(cfg["appearance"], "Systemowy"))
        self.look.grid(row=0, column=1, sticky="w", pady=(18, 8))

        ctk.CTkLabel(self, text="Opcje nagrywania, transkrypcji i konwersji są w każdej zakładce "
                                "pod przyciskiem ⚙ Opcje.",
                     text_color=MUTED, font=ctk.CTkFont(size=11), wraplength=470, justify="left"
                     ).grid(row=1, column=0, columnspan=2, sticky="w", padx=18, pady=(8, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=2, column=0, columnspan=2, sticky="e", padx=18, pady=(24, 12))
        ctk.CTkButton(btns, text="Anuluj", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Zapisz", width=90, command=self._save).pack(side="left")

    def _save(self):
        cfg = self.app.cfg
        cfg["appearance"] = {"Systemowy": "system", "Jasny": "light", "Ciemny": "dark"}[self.look.get()]
        ctk.set_appearance_mode(cfg["appearance"])
        save_config(cfg)
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════
#  Główne okno
# ══════════════════════════════════════════════════════════════════════════
class App(ctk.CTk):
    def __init__(self):
        self.cfg = load_config()
        ctk.set_appearance_mode(self.cfg["appearance"])
        ctk.set_default_color_theme("blue")
        global _ROOT_POS
        pos = self.cfg.get("window_pos")
        if (isinstance(pos, list) and len(pos) == 2
                and _monitor_at(pos[0] + 60, pos[1] + 20, 0)):     # tylko jeśli ten monitor nadal jest
            _ROOT_POS = (pos[0] + 60, pos[1] + 20)
        else:
            pos = None
        super().__init__(fg_color=BG)
        self.withdraw()                     # pokaż dopiero gotowe – bez przeskoków rozmiaru
        self.title(APP_NAME)
        self.geometry("320x420" + (f"+{pos[0]}+{pos[1]}" if pos else ""))
        self.minsize(320, 420)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass
        self._ui_q = queue.Queue()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(4, 0))
        ctk.CTkLabel(header, text=APP_NAME, height=22, font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="⚙", width=24, height=22, corner_radius=11, font=ctk.CTkFont(size=13),
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      command=lambda: SettingsWindow(self)).pack(side="right")
        self.pin_btn = ctk.CTkButton(header, text="📌", width=24, height=22, corner_radius=11,
                                     font=ctk.CTkFont(size=11), text_color=MUTED, command=self.toggle_on_top)
        self.pin_btn.pack(side="right", padx=(0, 4))
        self._apply_on_top()

        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_font=ctk.CTkFont(size=11, weight="bold"))
        self.tabs.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for name in ("Nagrywanie", "Transkrypcja", "Konwersja"):
            self.tabs.add(name)
            self.tabs.tab(name).grid_columnconfigure(0, weight=1)
            self.tabs.tab(name).grid_rowconfigure(0, weight=1)

        self.rec_tab = RecordTab(self.tabs.tab("Nagrywanie"), self)
        self.rec_tab.grid(row=0, column=0, sticky="nsew")
        self.tr_tab = TranscribeTab(self.tabs.tab("Transkrypcja"), self)
        self.tr_tab.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.conv_tab = ConvertTab(self.tabs.tab("Konwersja"), self)
        self.conv_tab.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-r>", lambda e: self.rec_tab.toggle())
        self.after(100, self._pump)
        self.after(200, self._reveal)
        self.after(100, self._fast_tick)
        self.after(500, self._slow_tick)

    def _reveal(self):
        global _ROOT_POS
        self.deiconify()
        _ROOT_POS = None
        self.after(30, lambda: _fix_client_size(self, 320, 420) if self._current_width <= 330 else _fix_client_size(self))
        self.after(80, lambda: _fit_on_monitor(self))

    def toggle_on_top(self):
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        save_config(self.cfg)
        self._apply_on_top()

    def _apply_on_top(self):
        on = bool(self.cfg["always_on_top"])
        self.attributes("-topmost", on)
        self.pin_btn.configure(fg_color=PIN_ON if on else "transparent",
                               hover_color=PIN_ON if on else CARD)

    def ui(self, fn):
        """Bezpieczne wywołanie kodu GUI z innego wątku."""
        self._ui_q.put(fn)

    def _pump(self):
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    log_exc("ui callback")
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _fast_tick(self):
        try:
            self.rec_tab.tick()
        except Exception:
            log_exc("rec tick")
        self.update_title()
        self.after(100, self._fast_tick)

    def _slow_tick(self):
        try:
            self.rec_tab.blink_tick()
            self.tr_tab.tick()
        except Exception:
            log_exc("slow tick")
        self.after(600, self._slow_tick)

    def update_title(self):
        r = self.rec_tab.rec
        t = f"● {fmt_hms(r.elapsed)} – {APP_NAME}" if r else APP_NAME
        if self.title() != t:
            self.title(t)

    def on_close(self):
        busy = []
        if self.rec_tab.is_recording():
            busy.append("trwa nagrywanie (zostanie zatrzymane i zapisane)")
        if self.tr_tab.busy:
            busy.append("trwa transkrypcja (stan zostanie zapisany do wznowienia)")
        if self.conv_tab.busy:
            busy.append("trwa konwersja (bieżący plik zostanie przerwany)")
        if self.rec_tab.processing:
            busy.append("trwa obróbka nagrania (zostanie surowe nagranie)")
        if busy and not messagebox.askyesno(APP_NAME, "Na pewno zamknąć?\n\n• " + "\n• ".join(busy), parent=self):
            return
        if self.rec_tab.is_recording():
            self.rec_tab.stop()
        self.tr_tab.shutdown()
        self.conv_tab.cancel.set()
        self.rec_tab.post_cancel.set()
        for t in self.rec_tab.post_threads:     # proces obróbki dostaje kill w ciągu ~0,5 s
            t.join(timeout=3)
        self.rec_tab.stop_monitor()
        try:
            _, _, x, y = self._parse_geometry_string(self.geometry())
            self.cfg["window_pos"] = [x, y]
            save_config(self.cfg)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        log_exc("main")
        raise
