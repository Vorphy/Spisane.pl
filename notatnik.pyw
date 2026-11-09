"""
Notatnik spotkań – nagrywanie rozmów (głośniki + mikrofon) do Opus,
konwersja starych nagrań do Opus i kolejka transkrypcji Whisperem
(wbudowana: engine.py + worker.py – dawniej zewnętrzny skrypt fw-dd.py).

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
    "agc": True,
    "pause_while_recording": True,
    "delete_original": False,
    "level_convert": True,
    "level_transcribe": True,
    "denoise_convert": True,
    "denoise_transcribe": True,
    "convert_cores": "auto",     # auto / p / e / all – też przygotowanie dźwięku do transkrypcji
    "appearance": "system",
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
    cfg["tr_opts"] = {**engine.DEFAULT_OPTIONS, **{k: v for k, v in (cfg.get("tr_opts") or {}).items()
                                                   if k in engine.DEFAULT_OPTIONS}}
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

        bar = self.toolbar = ctk.CTkFrame(self, fg_color="transparent")
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

        ctk.CTkButton(self.toolbar, text="⚙ Opcje", width=86, fg_color="transparent", border_width=1,
                      border_color=CARD_BORDER, text_color=("gray20", "gray80"), hover_color=CARD,
                      command=lambda: TranscribeOptions(self.app, self)).pack(side="right")
        self.summary = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=11),
                                    cursor="hand2", wraplength=540, justify="left")
        self.summary.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.summary.bind("<Button-1>", lambda e: TranscribeOptions(self.app, self))
        self.update_summary()

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.live = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED,
                                 font=ctk.CTkFont(size=12, slant="italic"))
        self.live.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.watch_sw = ctk.CTkSwitch(foot, text="", command=self._watch_changed)
        self.watch_sw.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._watch_label()
        self.pause_sw = ctk.CTkSwitch(foot, text="Nie zaczynaj nowego pliku podczas nagrywania",
                                      command=self._pause_changed)
        if app.cfg["pause_while_recording"]:
            self.pause_sw.select()
        self.pause_sw.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.level_sw = ctk.CTkSwitch(foot, text="Wyrównaj głośność przed transkrypcją",
                                      command=self._level_changed)
        if app.cfg["level_transcribe"]:
            self.level_sw.select()
        self.level_sw.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.nr_sw = ctk.CTkSwitch(foot, text="Usuń szum tła przed transkrypcją", command=self._level_changed)
        if app.cfg["denoise_transcribe"]:
            self.nr_sw.select()
        self.nr_sw.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.btn = ctk.CTkButton(foot, text="▶  Transkrybuj", width=150, height=36,
                                 font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle)
        self.btn.grid(row=1, column=1, rowspan=4, sticky="e", pady=(6, 0))

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
        self.summary.configure(text="Opcje:  " + "  ·  ".join(parts) + "   (kliknij, żeby zmienić)")

    def _watch_label(self):
        d = Path(self.app.cfg["watch_dir"])
        self.watch_sw.configure(text=f"Monitoruj folder „{d.name or d}” (nowe nagrania od razu do transkrypcji)")

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
        if not hasattr(row, "limit"):
            row.limit = ctk.CTkEntry(row, width=110, height=24, font=ctk.CTkFont(size=11),
                                     placeholder_text="✂ przytnij do…")
            row.limit.grid(row=1, column=1, sticky="e", padx=4)
            row.limit.bind("<FocusOut>", lambda e, r=row: self._check_limit(r))
            row.limit.bind("<Return>", lambda e, r=row: self._check_limit(r))
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
        limit = self._check_limit(row)
        if limit is None:
            row.set_status("błąd", "Przytnij do: wpisz minuty (np. 45) albo GG:MM:SS (np. 01:15:00)")
            self.after(10, self.tick)
            return
        row.limit_value = limit
        row.limit.configure(state="disabled")
        row.set_status("w toku")
        row.action.configure(state="disabled")
        level = self.app.cfg["level_transcribe"]
        denoise = self.app.cfg["denoise_transcribe"]
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
        self.transient(app)
        self.after(50, self.grab_set)
        if ICON_PATH.exists():
            self.after(250, lambda: self.iconbitmap(str(ICON_PATH)))
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
                    "Grafika Intel Iris Xe: ok. 5–7× szybciej, niż trwa nagranie (beam 1); fragmenty pominięte "
                    "przez model wykrywa VAD i transkrybuje ponownie. Bez grafiki Intel samo przejdzie na procesor.\n"
                    "Procesor: ok. 3× wolniej, obsługuje beam 2+ – dla ważnych nagrań: Procesor + beam 2.\n"
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
        if self.watch_dir.get() != self.app.cfg["watch_dir"]:
            self.app.cfg["watch_dir"] = self.watch_dir.get()
            self.tab.ignored.clear()
            self.tab._watch_label()
        save_config(self.app.cfg)
        self.tab.update_summary()
        self.destroy()


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
        cf = ctk.CTkFrame(foot, fg_color="transparent")
        cf.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ctk.CTkLabel(cf, text="Rdzenie CPU:").pack(side="left", padx=(0, 8))
        self.cores_sb = ctk.CTkSegmentedButton(cf, values=list(CONVERT_CORES.values()),
                                               command=self._cores_changed)
        self.cores_sb.set(CONVERT_CORES.get(app.cfg["convert_cores"], CONVERT_CORES["auto"]))
        self.cores_sb.pack(side="left")
        self.cores_hint = ctk.CTkLabel(foot, text="", anchor="w", text_color=MUTED, justify="left")
        self.cores_hint.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self._cores_hint()
        self.btn = ctk.CTkButton(foot, text="▶  Konwertuj", width=150, height=36,
                                 font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle)
        self.btn.grid(row=1, column=1, rowspan=3, sticky="e", pady=(6, 0))

    def _cores_changed(self, label):
        self.app.cfg["convert_cores"] = {v: k for k, v in CONVERT_CORES.items()}[label]
        save_config(self.app.cfg)
        self._cores_hint()

    def _cores_hint(self, now=None):
        hint = {"auto": "E podczas nagrywania, P poza nim – przełącza się w trakcie pliku.",
                "p": "Najszybciej (ok. 2× szybciej niż E), ale konkuruje z aplikacjami na pierwszym planie.",
                "e": "Wolniej, za to rdzenie P zostają wolne dla Teams i innych aplikacji.",
                "all": "Windows sam przydziela – konwersja i tak używa jednego wątku."
                }.get(self.app.cfg["convert_cores"], "")
        hint += "  Dotyczy też przygotowania dźwięku do transkrypcji."
        if now:
            hint = f"Teraz: rdzenie {now}.   " + hint
        self.cores_hint.configure(text=hint)

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
                dst = convproc.convert(
                    row.path, bitrate=bitrate, level=level, denoise=denoise,
                    cores=self.app.cfg["convert_cores"], recording=self.app.rec_tab.is_recording,
                    on_cores=lambda k, d: ui(lambda: self._cores_hint(CORE_NAMES.get(k, k))),
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
        self._cores_hint()
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
        row(0, "Folder nagrań", self.save_dir,
            lambda: self._pick_dir(self.save_dir))
        ctk.CTkLabel(self, text="Model, urządzenie, format itd.: zakładka Transkrypcja → ⚙ Opcje",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).grid(row=1, column=1, columnspan=2, sticky="w")

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

    def _save(self):
        cfg = self.app.cfg
        cfg["save_dir"] = self.save_dir.get()
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
        self.tr_tab.shutdown()
        self.conv_tab.cancel.set()
        self.rec_tab.stop_monitor()
        self.destroy()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        log_exc("main")
        raise
