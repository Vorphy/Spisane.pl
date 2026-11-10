"""
tray.py – ikona w zasobniku, autostart z Windows, jedna kopia aplikacji, praca w tle bez przyhamowania.

Wszystko tu jest „miękkie”: gdy coś się nie uda (brak pystray, inny system), aplikacja działa dalej
jak dotąd, tylko bez danej funkcji.
"""

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NotatnikSpotkan"
TRAY_ARG = "--tray"                    # start schowany w zasobniku (autostart)
_MUTEX_NAME = "Local\\NotatnikSpotkan.instancja"
_SHOW_EVENT = "Local\\NotatnikSpotkan.pokaz"

_k32 = ctypes.windll.kernel32 if os.name == "nt" else None


# ── jedna kopia ──────────────────────────────────────────────────────────────
class SingleInstance:
    """Pierwsza kopia trzyma muteks i nasłuchuje zdarzenia „pokaż się”.
    Druga kopia (np. ręczne uruchomienie przy działającym autostarcie) tylko budzi pierwszą i kończy."""

    def __init__(self):
        self.first = True
        self._event = None
        if not _k32:
            return
        _k32.CreateMutexW.restype = wintypes.HANDLE
        _k32.CreateEventW.restype = wintypes.HANDLE
        _k32.OpenEventW.restype = wintypes.HANDLE
        self._mutex = _k32.CreateMutexW(None, False, _MUTEX_NAME)
        if ctypes.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            self.first = False
            ev = _k32.OpenEventW(0x0002, False, _SHOW_EVENT)   # EVENT_MODIFY_STATE
            if ev:
                _k32.SetEvent(wintypes.HANDLE(ev))
                _k32.CloseHandle(wintypes.HANDLE(ev))
            return
        self._event = _k32.CreateEventW(None, False, False, _SHOW_EVENT)   # auto-reset

    def show_requested(self):
        """True, jeśli inna kopia poprosiła o pokazanie okna (sprawdzane cyklicznie, nie blokuje)."""
        return bool(self._event) and _k32.WaitForSingleObject(wintypes.HANDLE(self._event), 0) == 0


# ── praca w tle bez przyhamowania ────────────────────────────────────────────
class _PPT(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def no_power_throttling(handle=None):
    """Wyłącza tryb wydajności Windows 11 (EcoQoS) dla procesu (domyślnie bieżącego).
    Windows włącza go oknom zminimalizowanym i procesom w tle: niskie taktowanie, rzadsze budzenie
    timerów. Dla nagrywania i obróbki to zbędne hamowanie – aplikacja ma być traktowana jak aktywna."""
    if not _k32:
        return False
    try:
        _k32.GetCurrentProcess.restype = wintypes.HANDLE
        _k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        _k32.SetProcessInformation.restype = wintypes.BOOL
        h = _k32.GetCurrentProcess() if handle is None else wintypes.HANDLE(int(handle))
        # 0x1 = EXECUTION_SPEED, 0x4 = IGNORE_TIMER_RESOLUTION (Windows 11; starsze systemy – samo 0x1)
        for mask in (0x5, 0x1):
            st = _PPT(1, mask, 0)
            if _k32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st)):   # ProcessPowerThrottling
                return True
    except Exception:
        pass
    return False


def on_battery():
    """True, gdy laptop działa z baterii (bez zasilacza)."""
    if not _k32:
        return False

    class _SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
    s = _SPS()
    try:
        return bool(_k32.GetSystemPowerStatus(ctypes.byref(s))) and s.ACLineStatus == 0
    except Exception:
        return False


def keep_awake(on):
    """Komputer nie zaśnie (ekran może się wygasić), dopóki trwa nagrywanie lub obróbka."""
    if not _k32:
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        _k32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


# ── autostart ────────────────────────────────────────────────────────────────
def _launch_command(minimized=True):
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    if w.exists():
        exe = w
    cmd = f'"{exe}" "{APP_DIR / "notatnik.pyw"}"'
    return cmd + (f" {TRAY_ARG}" if minimized else "")


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(on, minimized=True):
    """Wpis w HKCU\\...\\Run – tylko dla tego użytkownika, bez uprawnień administratora."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _launch_command(minimized))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


# ── ikona zawsze widoczna (Windows 11) ───────────────────────────────────────
_ICON_SETTINGS = r"Control Panel\NotifyIconSettings"


def promote_icon(tooltip):
    """Ikona na pasku zadań zamiast w rozwijanym menu „^”. Windows 11 pamięta to per ikona w
    HKCU\\Control Panel\\NotifyIconSettings\\<id>\\IsPromoted (to samo, co przeciągnięcie ikony myszą).
    Wpis powstaje dopiero po pierwszym pokazaniu ikony – stąd wywołanie po starcie.
    Zwraca liczbę zmienionych wpisów."""
    try:
        import winreg
    except ImportError:
        return 0
    changed = 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ICON_SETTINGS) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                        def val(v):
                            try:
                                return winreg.QueryValueEx(k, v)[0]
                            except OSError:
                                return None
                        exe = str(val("ExecutablePath") or "").lower()
                        if val("InitialTooltip") != tooltip or "python" not in exe:
                            continue
                        if val("IsPromoted") != 1:
                            winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                            changed += 1
                except OSError:
                    continue
    except OSError:
        pass
    return changed


# ── ikona w zasobniku ────────────────────────────────────────────────────────
class Tray:
    """Ikona w zasobniku (pystray we własnym wątku). Akcje wracają do wątku okna przez ui(fn).
    actions: {"show", "toggle_rec", "quit"}; is_recording() – do napisu w menu."""

    def __init__(self, title, icon_path, ui, actions, is_recording, always_visible=True):
        self.ok = False
        self._ui, self._act, self._is_rec = ui, actions, is_recording
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        try:
            base = Image.open(icon_path).convert("RGBA") if Path(icon_path).exists() else \
                Image.new("RGBA", (64, 64), (37, 99, 235, 255))
            base = base.resize((64, 64))
        except Exception:
            base = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
        rec = base.copy()
        d = ImageDraw.Draw(rec)
        d.ellipse((30, 30, 63, 63), fill=(229, 72, 77, 255), outline=(255, 255, 255, 255), width=3)
        self._img_idle, self._img_rec = base, rec
        self._recording = False

        def call(name):
            return lambda icon, item: self._ui(self._act[name])

        menu = pystray.Menu(
            pystray.MenuItem("Pokaż okno", call("show"), default=True),   # też dwuklik / klik w ikonę
            pystray.MenuItem(lambda item: "■ Zatrzymaj nagrywanie" if self._is_rec() else "● Nagrywaj",
                             call("toggle_rec")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Zakończ", call("quit")),
        )
        self.icon = pystray.Icon(RUN_VALUE, base, title, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.ok = True
        if always_visible:
            def promote():
                import time
                for delay in (2, 8):            # wpis w rejestrze Windows tworzy chwilę po pokazaniu ikony
                    time.sleep(delay)
                    promote_icon(title)
            threading.Thread(target=promote, daemon=True).start()

    def update(self, tooltip, recording):
        if not self.ok:
            return
        try:
            if recording != self._recording:
                self._recording = recording
                self.icon.icon = self._img_rec if recording else self._img_idle
                self.icon.update_menu()
            if self.icon.title != tooltip:
                self.icon.title = tooltip[:120]
        except Exception:
            pass

    def notify(self, text, title=""):
        if self.ok:
            try:
                self.icon.notify(text, title)
            except Exception:
                pass

    def stop(self):
        if self.ok:
            try:
                self.icon.stop()
            except Exception:
                pass
