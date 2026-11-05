"""
Notatnik spotkań – nagrywanie rozmów (głośniki + mikrofon) do Opus,
konwersja starych nagrań do Opus i kolejka transkrypcji przez fw-dd.py.

Uruchomienie: pythonw notatnik.pyw   (albo skrót „Notatnik spotkań”)
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import deps          # noqa: E402  – brakujące pakiety (nowy komputer): zapytaj i doinstaluj
deps.ensure()

import customtkinter as ctk  # noqa: E402

import recorder      # noqa: E402
import converter     # noqa: E402
import transcriber   # noqa: E402

APP_NAME = "Notatnik spotkań"
CONFIG_PATH = APP_DIR / "config.json"
ICON_PATH = APP_DIR / "notatnik.ico"
LOG_PATH = APP_DIR / "notatnik.log"
LEVEL_DIR = APP_DIR / "_wyrownane"   # tymczasowe kopie z wyrównaną głośnością do transkrypcji

DEFAULTS = {
    "save_dir": str(Path.home() / "Videos"),
    # fw-dd.py obok aplikacji (repo), a jeśli go tam nie ma – folder wyżej (stary układ)
    "fwdd_path": str(APP_DIR / "fw-dd.py" if (APP_DIR / "fw-dd.py").exists() else APP_DIR.parent / "fw-dd.py"),
    "mic_name": None,
    "loop_name": None,
    "bitrate": 32000,
    "agc": True,
    "pause_while_recording": True,
    "delete_original": False,
    "level_convert": True,
    "level_transcribe": True,
    "denoise_convert": True,
    "denoise_transcribe": True,
    "appearance": "system",
}

CONVERT_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".wma", ".ogg"}

# ── Kolory ────────────────────────────────────────────────────────────────
RED = ("#E5484D", "#E5484D")
RED_HOVER = ("#C93C40", "#F2555A")
GREEN = ("#2F9E62", "#3DD68C")
MUTED = ("#6B7280", "#9CA3AF")
CARD = ("#F3F4F6", "#23262B")
CARD_BORDER = ("#E5E7EB", "#30343A")
BG = ("#FFFFFF", "#17191C")

STATUS_COLORS = {
    "czeka": MUTED, "w toku": ("#2563EB", "#60A5FA"), "gotowe": GREEN,
    "zatrzymano": ("#B45309", "#FBBF24"), "błąd": RED, "do wznowienia": ("#B45309", "#FBBF24"),
}


def log_exc(where):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {where}\n{traceback.format_exc()}")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


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

        self.name = ctk.CTkLabel(self, text=self.path.name, anchor="w",
                                 font=ctk.CTkFont(size=13, weight="bold"))
        self.name.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=(8, 0))
        self.detail = ctk.CTkLabel(self, text=str(self.path.parent), anchor="w",
                                   text_color=MUTED, font=ctk.CTkFont(size=11))
        self.detail.grid(row=1, column=0, sticky="ew", padx=(12, 6))

        self.status_lbl = ctk.CTkLabel(self, text="czeka", text_color=MUTED,
                                       font=ctk.CTkFont(size=12, weight="bold"), width=90, anchor="e")
        self.status_lbl.grid(row=0, column=1, sticky="e", padx=4, pady=(8, 0))

        self.action = ctk.CTkButton(self, text="✕", width=28, height=28, corner_radius=8,
                                    fg_color="transparent", hover_color=CARD_BORDER,
                                    text_color=MUTED, command=lambda: on_remove(self))
        self.action.grid(row=0, column=2, rowspan=2, padx=(0, 8), pady=6)

        self.bar = ctk.CTkProgressBar(self, height=6, corner_radius=3)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 10))

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

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkButton(bar, text="＋ Pliki…", width=96, command=self.add_files).pack(side="left")
        ctk.CTkButton(bar, text="＋ Folder…", width=96, command=self.add_folder).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="Wyczyść gotowe", width=110, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"),
                      hover_color=CARD, command=self.clear_done).pack(side="left")

        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=1, column=0, sticky="nsew")
        self.list.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(self.list, text=empty_text, text_color=MUTED, justify="center")
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

    def next_waiting(self):
        return next((r for r in self.rows if r.status in ("czeka", "do wznowienia")), None)


# ══════════════════════════════════════════════════════════════════════════
#  Zakładka: Transkrypcja
# ══════════════════════════════════════════════════════════════════════════
class TranscribeTab(FileQueue):
    def __init__(self, master, app):
        super().__init__(master, app, "Dodaj nagrania albo cały folder.\n"
                                      "Z folderu trafią tu pliki bez transkrypcji (.txt).")
        self.running = False
        self.job = None
        self.current = None
        self.preparing = None              # wiersz, dla którego trwa wyrównywanie głośności
        self.prep_cancel = threading.Event()

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.live = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED,
                                 font=ctk.CTkFont(size=12, slant="italic"))
        self.live.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.pause_sw = ctk.CTkSwitch(foot, text="Nie zaczynaj nowego pliku podczas nagrywania",
                                      command=self._pause_changed)
        if app.cfg["pause_while_recording"]:
            self.pause_sw.select()
        self.pause_sw.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.level_sw = ctk.CTkSwitch(foot, text="Wyrównaj głośność przed transkrypcją",
                                      command=self._level_changed)
        if app.cfg["level_transcribe"]:
            self.level_sw.select()
        self.level_sw.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.nr_sw = ctk.CTkSwitch(foot, text="Usuń szum tła przed transkrypcją", command=self._level_changed)
        if app.cfg["denoise_transcribe"]:
            self.nr_sw.select()
        self.nr_sw.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.btn = ctk.CTkButton(foot, text="▶  Transkrybuj", width=150, height=36,
                                 font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle)
        self.btn.grid(row=1, column=1, rowspan=3, sticky="e", pady=(6, 0))

    def _level_changed(self):
        self.app.cfg["level_transcribe"] = bool(self.level_sw.get())
        self.app.cfg["denoise_transcribe"] = bool(self.nr_sw.get())
        save_config(self.app.cfg)

    def _pause_changed(self):
        self.app.cfg["pause_while_recording"] = bool(self.pause_sw.get())
        save_config(self.app.cfg)

    def filetypes(self):
        return [("Nagrania", " ".join("*" + e for e in sorted(transcriber.AUDIO_EXTENSIONS))),
                ("Wszystkie pliki", "*.*")]

    def scan(self, folder):
        rec = self.app.rec_tab.current_path()
        return transcriber.scan_folder(folder, exclude=[rec] if rec else [])

    def on_added(self, row):
        if (row.path.with_suffix(".transcribe_state").exists()
                or leveled_tmp(row.path).with_suffix(".transcribe_state").exists()):
            row.set_status("do wznowienia")

    def toggle(self):
        if self.running:
            self.running = False
            if self.preparing:
                self.prep_cancel.set()
                self.btn.configure(text="Zatrzymuję…", state="disabled")
            elif self.job:
                self.btn.configure(text="Zatrzymuję…", state="disabled")
                self.live.configure(text="Zapisuję stan, żeby można było później wznowić…")
                self.job.stop()
            else:
                self._idle()
        else:
            if not Path(self.app.cfg["fwdd_path"]).exists():
                messagebox.showerror(APP_NAME, f"Nie znaleziono skryptu:\n{self.app.cfg['fwdd_path']}\n\n"
                                               "Wskaż go w Ustawieniach.", parent=self)
                return
            if not self.next_waiting():
                messagebox.showinfo(APP_NAME, "Brak plików oczekujących na transkrypcję.", parent=self)
                return
            self.running = True
            self.btn.configure(text="■  Zatrzymaj", fg_color=RED, hover_color=RED_HOVER)
            self.tick()

    def _idle(self):
        self.btn.configure(text="▶  Transkrybuj", state="normal",
                           fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                           hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])

    def tick(self):
        """Wywoływane cyklicznie – startuje kolejny plik, gdy można."""
        if not self.running or self.job or self.preparing:
            return
        row = self.next_waiting()
        if not row:
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
        self.current = row
        row.set_status("w toku")
        row.action.configure(state="disabled")
        level = self.app.cfg["level_transcribe"]
        denoise = self.app.cfg["denoise_transcribe"]
        if not (level or denoise):
            self._launch(row, row.path)
            return
        tmp = leveled_tmp(row.path)
        if tmp.exists() and tmp.with_suffix(".transcribe_state").exists():
            self._launch(row, tmp)            # wznowienie – kopia już jest
            return
        # wyrównanie głośności w tle, potem transkrypcja kopii
        self.preparing = row
        self.prep_cancel.clear()
        self.live.configure(text=" i ".join(x for x, on in (("Wyrównuję głośność", level),
                                                                 ("usuwam szum" if level else "Usuwam szum", denoise)) if on) + "…")
        ui = self.app.ui

        def work():
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                converter.convert_to_opus(row.path, tmp, bitrate=48000, level=level, denoise=denoise,
                                          progress=lambda f: ui(lambda: row.set_progress(f)),
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
            row.set_status("czeka")
            row.set_progress(0)
            row.action.configure(state="normal")
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
            row.set_status("czeka")
            row.action.configure(state="normal")
            self._idle()
            return
        self._launch(row, tmp)

    def _launch(self, row, audio):
        self.live.configure(text="Ładowanie modelu…")
        ui = self.app.ui
        self.job = transcriber.TranscribeJob(
            self.app.cfg["fwdd_path"], audio,
            on_progress=lambda f: ui(lambda: row.set_progress(f)),
            on_line=lambda s: ui(lambda: self.live.configure(text="„" + s[:110] + "”")),
            on_done=lambda st, p: ui(lambda: self._done(row, st, p, audio)))
        try:
            self.job.start()
        except Exception as e:
            log_exc("transcribe start")
            self.job = None
            row.set_status("błąd", str(e))
            row.action.configure(state="normal")

    def _finish_leveled(self, row, tmp_txt):
        """Kopia wyrównana przetranskrybowana -> .txt i oryginał trafiają tam, gdzie zrobiłby to fw-dd.py."""
        import shutil
        orig = row.path
        dest_dir = orig.parent if orig.parent.name == "przetworzone" else orig.parent / "przetworzone"
        dest_dir.mkdir(exist_ok=True)
        txt = dest_dir / (orig.stem + ".txt")
        shutil.move(str(tmp_txt), str(txt))
        for ext in (".srt", ".vtt"):
            extra = tmp_txt.with_suffix(ext)
            if extra.exists():
                shutil.move(str(extra), str(dest_dir / (orig.stem + ext)))
        if orig.parent != dest_dir and orig.exists():
            shutil.move(str(orig), str(dest_dir / orig.name))
        shutil.rmtree(leveled_tmp(orig).parent, ignore_errors=True)
        return txt

    def _done(self, row, status, txt, audio=None):
        if status == "done" and txt and audio is not None and Path(audio) != row.path:
            try:
                txt = self._finish_leveled(row, Path(txt))
            except Exception:
                log_exc("finish leveled")
        log_tail = self.job.log[-40:] if self.job else []
        self.job = None
        row.action.configure(state="normal")
        if status == "done":
            row.set_status("gotowe", f"→ {txt}" if txt else None)
            if txt:
                row.result = txt
                row.set_action("Otwórz", lambda p=txt: os.startfile(str(p)), width=70)
        elif status == "stopped":
            row.set_status("do wznowienia", "Zatrzymano – stan zapisany, kliknij Transkrybuj, by wznowić")
            self.live.configure(text="Zatrzymano.")
        else:
            row.set_status("błąd", "Szczegóły w notatnik.log")
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Transkrypcja nieudana: {row.path}\n")
                    f.write("\n".join(log_tail) + "\n")
            except OSError:
                pass
        if not self.running:
            self._idle()
        self.tick()

    @property
    def busy(self):
        return self.job is not None or self.preparing is not None


# ══════════════════════════════════════════════════════════════════════════
#  Zakładka: Konwersja
# ══════════════════════════════════════════════════════════════════════════
class ConvertTab(FileQueue):
    def __init__(self, master, app):
        super().__init__(master, app, "Dodaj nagrania MP4 (np. z OBS) albo cały folder.\n"
                                      "Powstanie plik .opus obok – wielokrotnie mniejszy.")
        self.running = False
        self.worker = None
        self.cancel = threading.Event()
        self.saved_total = 0      # faktycznie odzyskane (usunięte oryginały)
        self.potential = 0        # do odzyskania po ręcznym usunięciu oryginałów

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.info = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED)
        self.info.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.del_cb = ctk.CTkCheckBox(foot, text="Usuń oryginał po udanej konwersji",
                                      command=self._del_changed)
        if app.cfg["delete_original"]:
            self.del_cb.select()
        self.del_cb.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.level_cb = ctk.CTkCheckBox(foot, text="Wyrównaj głośność (cisi rozmówcy podgłośnieni)",
                                        command=self._level_changed)
        if app.cfg["level_convert"]:
            self.level_cb.select()
        self.level_cb.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.nr_cb = ctk.CTkCheckBox(foot, text="Usuń szum tła", command=self._level_changed)
        if app.cfg["denoise_convert"]:
            self.nr_cb.select()
        self.nr_cb.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.btn = ctk.CTkButton(foot, text="▶  Konwertuj", width=150, height=36,
                                 font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle)
        self.btn.grid(row=1, column=1, rowspan=3, sticky="e", pady=(6, 0))

    def _level_changed(self):
        self.app.cfg["level_convert"] = bool(self.level_cb.get())
        self.app.cfg["denoise_convert"] = bool(self.nr_cb.get())
        save_config(self.app.cfg)

    def _del_changed(self):
        self.app.cfg["delete_original"] = bool(self.del_cb.get())
        save_config(self.app.cfg)

    def filetypes(self):
        return [("Nagrania wideo/audio", " ".join("*" + e for e in sorted(CONVERT_EXT))),
                ("Wszystkie pliki", "*.*")]

    def scan(self, folder):
        return [f for f in sorted(Path(folder).iterdir())
                if f.is_file() and f.suffix.lower() in CONVERT_EXT and not f.with_suffix(".opus").exists()]

    def on_added(self, row):
        try:
            row.detail.configure(text=f"{row.path.parent}   ·   {fmt_size(row.path.stat().st_size)}")
        except OSError:
            pass

    def toggle(self):
        if self.running:
            self.cancel.set()
            self.btn.configure(text="Zatrzymuję…", state="disabled")
            return
        if not self.next_waiting():
            messagebox.showinfo(APP_NAME, "Brak plików do konwersji.", parent=self)
            return
        if self.app.cfg["delete_original"] and not messagebox.askyesno(
                APP_NAME, "Oryginały zostaną usunięte po udanej konwersji (sprawdzam długość nagrania).\n"
                          "Obraz z plików wideo przepadnie. Kontynuować?", parent=self):
            return
        self.running = True
        self.cancel.clear()
        self.btn.configure(text="■  Zatrzymaj", fg_color=RED, hover_color=RED_HOVER)
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _work(self):
        ui = self.app.ui
        delete = self.app.cfg["delete_original"]
        bitrate = self.app.cfg["bitrate"]
        level = self.app.cfg["level_convert"]
        denoise = self.app.cfg["denoise_convert"]
        while not self.cancel.is_set():
            row = self.next_waiting()
            if not row:
                break
            ui(lambda r=row: (r.set_status("w toku"), r.action.configure(state="disabled")))
            try:
                before = row.path.stat().st_size
                dst = converter.convert_to_opus(
                    row.path, bitrate=bitrate, level=level, denoise=denoise,
                    progress=lambda f, r=row: ui(lambda: r.set_progress(f)),
                    cancel=self.cancel.is_set)
                after = dst.stat().st_size
                note = f"{fmt_size(before)} → {fmt_size(after)}"
                if delete:
                    if converter.verify(row.path, dst):
                        row.path.unlink()
                        self.saved_total += before - after
                        note += "   ·   oryginał usunięty"
                    else:
                        note += "   ·   długość się nie zgadza – oryginał zostawiony!"
                else:
                    self.potential += before - after
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
        self.btn.configure(text="▶  Konwertuj", state="normal",
                           fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                           hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])
        parts = []
        if self.saved_total > 0:
            parts.append(f"Odzyskane miejsce: {fmt_size(self.saved_total)}")
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
        top.grid(row=0, column=0, pady=(18, 0))
        self.dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=30), text_color=MUTED)
        self.dot.pack(side="left", padx=(0, 10))
        self.timer = ctk.CTkLabel(top, text="00:00:00", font=ctk.CTkFont(family="Segoe UI", size=56, weight="bold"))
        self.timer.pack(side="left")
        self.state_lbl = ctk.CTkLabel(self, text="Gotowy do nagrywania", text_color=MUTED,
                                      font=ctk.CTkFont(size=13))
        self.state_lbl.grid(row=1, column=0)

        # ── Wskaźniki poziomu ──
        meters = ctk.CTkFrame(self, fg_color=CARD, border_color=CARD_BORDER, border_width=1, corner_radius=12)
        meters.grid(row=2, column=0, sticky="ew", padx=8, pady=(18, 0))
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

        ctk.CTkLabel(meters, text="🎙  Mikrofon", anchor="w", width=150).grid(
            row=0, column=0, sticky="w", padx=(14, 6), pady=(12, 0))
        self.mic_var = ctk.StringVar(value=pick(self.devs_in, app.cfg["mic_name"], def_in))
        self.mic_menu = ctk.CTkOptionMenu(meters, variable=self.mic_var, dynamic_resizing=False,
                                          values=[n for _, n in self.devs_in] or ["(brak urządzeń)"],
                                          command=lambda _: self._dev_changed())
        self.mic_menu.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(12, 0))
        self.mic_bar = ctk.CTkProgressBar(meters, height=8, progress_color=GREEN)
        self.mic_bar.set(0)
        self.mic_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 10))

        ctk.CTkLabel(meters, text="🔊  Rozmówcy", anchor="w", width=150).grid(
            row=2, column=0, sticky="w", padx=(14, 6))
        self.loop_var = ctk.StringVar(value=pick(self.devs_loop, app.cfg["loop_name"], def_loop))
        self.loop_menu = ctk.CTkOptionMenu(meters, variable=self.loop_var, dynamic_resizing=False,
                                           values=[n for _, n in self.devs_loop] or ["(brak urządzeń)"],
                                           command=lambda _: self._dev_changed())
        self.loop_menu.grid(row=2, column=1, sticky="ew", padx=(0, 14))
        self.loop_bar = ctk.CTkProgressBar(meters, height=8, progress_color=GREEN)
        self.loop_bar.set(0)
        self.loop_bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 4))
        self.check_btn = ctk.CTkButton(meters, text="Sprawdź poziomy (10 s)", width=170, height=26,
                                       fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                       text_color=("gray20", "gray85"), hover_color=CARD,
                                       command=self.start_monitor)
        self.check_btn.grid(row=4, column=0, columnspan=2, pady=(2, 12))

        # ── Przycisk ──
        self.btn = ctk.CTkButton(self, text="●   Nagrywaj", width=280, height=58, corner_radius=29,
                                 font=ctk.CTkFont(size=19, weight="bold"),
                                 fg_color=RED, hover_color=RED_HOVER, command=self.toggle)
        self.btn.grid(row=3, column=0, pady=(24, 10))

        # ── Info o pliku ──
        self.file_lbl = ctk.CTkLabel(self, text="", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.file_lbl.grid(row=4, column=0)
        links = ctk.CTkFrame(self, fg_color="transparent")
        links.grid(row=5, column=0, pady=(4, 0))
        self.to_tr = ctk.CTkButton(links, text="Dodaj do transkrypcji", width=160, height=30,
                                   fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                   text_color=("gray20", "gray85"), hover_color=CARD, command=self._to_transcribe)
        self.show = ctk.CTkButton(links, text="Pokaż w folderze", width=130, height=30,
                                  fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                                  text_color=("gray20", "gray85"), hover_color=CARD,
                                  command=lambda: reveal(self.last_file))
        self._links = links

        # Mikrofon NIE jest otwierany w tle – tylko podczas nagrywania
        # albo na 10 s po kliknięciu "Sprawdź poziomy" (żeby nie przeszkadzać Teams itp.).
        self.monitor = None
        self._monitor_job = None

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
            self.rec = recorder.Recorder(path, mic, loop, bitrate=self.app.cfg["bitrate"],
                                         agc=self.app.cfg["agc"])
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
            self.to_tr.configure(state="normal", text="Dodaj do transkrypcji")
            self._links.grid()
        self.app.update_title()

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
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Ustawienia")
        self.geometry("600x380")
        self.resizable(False, False)
        self.transient(app)
        self.after(50, self.grab_set)
        if ICON_PATH.exists():
            self.after(250, lambda: self.iconbitmap(str(ICON_PATH)))
        self.grid_columnconfigure(1, weight=1)
        cfg = app.cfg

        def row(r, label, var, browse):
            ctk.CTkLabel(self, text=label, anchor="w").grid(row=r, column=0, sticky="w", padx=(18, 8), pady=8)
            ctk.CTkEntry(self, textvariable=var).grid(row=r, column=1, sticky="ew", pady=8)
            ctk.CTkButton(self, text="…", width=36, command=browse).grid(row=r, column=2, padx=(6, 18), pady=8)

        self.save_dir = ctk.StringVar(value=cfg["save_dir"])
        self.fwdd = ctk.StringVar(value=cfg["fwdd_path"])
        row(0, "Folder nagrań", self.save_dir,
            lambda: self._pick_dir(self.save_dir))
        row(1, "Skrypt fw-dd.py", self.fwdd,
            lambda: self._pick_file(self.fwdd))

        ctk.CTkLabel(self, text="Jakość nagrania", anchor="w").grid(row=2, column=0, sticky="w", padx=(18, 8), pady=8)
        self.quality = ctk.CTkSegmentedButton(self, values=["24 kbps", "32 kbps", "48 kbps"])
        self.quality.set(f"{cfg['bitrate'] // 1000} kbps" if cfg["bitrate"] in (24000, 32000, 48000) else "32 kbps")
        self.quality.grid(row=2, column=1, sticky="w", pady=8)
        ctk.CTkLabel(self, text="32 kbps ≈ 14 MB na godzinę – w zupełności wystarcza do mowy",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).grid(row=3, column=1, columnspan=2, sticky="w")

        self.agc = ctk.CTkSwitch(self, text="Wyrównuj głośność (cisi rozmówcy podgłośnieni do poziomu mowy)")
        if cfg["agc"]:
            self.agc.select()
        self.agc.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 4))

        ctk.CTkLabel(self, text="Wygląd", anchor="w").grid(row=5, column=0, sticky="w", padx=(18, 8), pady=8)
        self.look = ctk.CTkSegmentedButton(self, values=["Systemowy", "Jasny", "Ciemny"])
        self.look.set({"system": "Systemowy", "light": "Jasny", "dark": "Ciemny"}.get(cfg["appearance"], "Systemowy"))
        self.look.grid(row=5, column=1, sticky="w", pady=8)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=6, column=0, columnspan=3, sticky="e", padx=18, pady=(18, 12))
        ctk.CTkButton(btns, text="Anuluj", width=90, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray85"), hover_color=CARD,
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Zapisz", width=90, command=self._save).pack(side="left")

    def _pick_dir(self, var):
        d = filedialog.askdirectory(parent=self, initialdir=var.get())
        if d:
            var.set(str(Path(d)))

    def _pick_file(self, var):
        f = filedialog.askopenfilename(parent=self, initialdir=str(Path(var.get()).parent),
                                       filetypes=[("Python", "*.py")])
        if f:
            var.set(str(Path(f)))

    def _save(self):
        cfg = self.app.cfg
        cfg["save_dir"] = self.save_dir.get()
        cfg["fwdd_path"] = self.fwdd.get()
        cfg["bitrate"] = int(self.quality.get().split()[0]) * 1000
        cfg["agc"] = bool(self.agc.get())
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
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        self.geometry("600x720")
        self.minsize(540, 660)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass
        self._ui_q = queue.Queue()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(header, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="⚙", width=34, height=34, corner_radius=17, font=ctk.CTkFont(size=17),
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      command=lambda: SettingsWindow(self)).pack(side="right")

        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_font=ctk.CTkFont(size=13, weight="bold"))
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(2, 12))
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
        self.after(100, self._fast_tick)
        self.after(500, self._slow_tick)

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
        if busy and not messagebox.askyesno(APP_NAME, "Na pewno zamknąć?\n\n• " + "\n• ".join(busy), parent=self):
            return
        if self.rec_tab.is_recording():
            self.rec_tab.stop()
        if self.tr_tab.job:
            self.tr_tab.running = False
            self.tr_tab.job.stop()
            proc = self.tr_tab.job.proc
            try:
                proc.wait(timeout=60)
            except Exception:
                pass
        self.conv_tab.cancel.set()
        self.rec_tab.stop_monitor()
        self.destroy()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        log_exc("main")
        raise
