"""
transcriber.py – uruchamia istniejący fw-dd.py w osobnym procesie (bez zmian w jego kodzie)
i tłumaczy jego wyjście na postęp dla okienka.

Zatrzymanie: zakładamy plik-flagę; mały "starter" wewnątrz procesu go widzi i wywołuje
Ctrl+C (KeyboardInterrupt), więc fw-dd.py zapisuje stan transkrypcji do wznowienia.
"""

import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus",
                    ".aac", ".wma", ".webm", ".mp4", ".mkv"}

# Kod startera uruchamiany w procesie potomnym
_STUB = r"""
import os, sys, runpy, threading, time, _thread
script, audio, flag = sys.argv[1], sys.argv[2], sys.argv[3]
def watch():
    while not os.path.exists(flag):
        time.sleep(0.5)
    _thread.interrupt_main()      # = Ctrl+C w fw-dd.py
threading.Thread(target=watch, daemon=True).start()
sys.argv = [script, audio]
runpy.run_path(script, run_name="__main__")
"""

_PCT = re.compile(r"\|\s*([\d.]+)%\s*\|")
_SEG = re.compile(r"^\[\s*[\d.]+s\]\s*(.+)$")


def python_console_exe():
    """python.exe (nie pythonw.exe) – potrzebny do procesu z przechwytywanym wyjściem."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def scan_folder(folder, exclude=()):
    """Jak tryb -B w fw-dd.py: pliki audio bez .txt obok albo z przerwanym stanem."""
    folder = Path(folder)
    exclude = {Path(e).resolve() for e in exclude}
    out = []
    for f in sorted(folder.iterdir()):
        if (f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                and f.parent.name != "przetworzone"
                and f.resolve() not in exclude
                and (not f.with_suffix(".txt").exists()
                     or f.with_suffix(".transcribe_state").exists())):
            out.append(f)
    return out


def result_txt(audio):
    """Gdzie fw-dd.py zostawi .txt (przenosi wszystko do podfolderu 'przetworzone')."""
    audio = Path(audio)
    for cand in (audio.parent / "przetworzone" / (audio.stem + ".txt"), audio.with_suffix(".txt")):
        if cand.exists():
            return cand
    return None


class TranscribeJob:
    """
    job = TranscribeJob(fwdd_path, audio, on_progress, on_line, on_done)
    on_progress(ułamek), on_line(tekst), on_done(status, txt_path) – wywoływane z wątku roboczego.
    status: "done" | "stopped" | "error"
    """

    def __init__(self, fwdd, audio, on_progress=None, on_line=None, on_done=None):
        self.fwdd = Path(fwdd)
        self.audio = Path(audio)
        self.on_progress = on_progress or (lambda f: None)
        self.on_line = on_line or (lambda s: None)
        self.on_done = on_done or (lambda st, p: None)
        self.proc = None
        self._flag = Path(tempfile.gettempdir()) / f"notatnik_stop_{os.getpid()}_{id(self)}.flag"
        self._stopping = False
        self.log = []

    def start(self):
        self._flag.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        self.proc = subprocess.Popen(
            [python_console_exe(), "-u", "-c", _STUB, str(self.fwdd), str(self.audio), str(self._flag)],
            cwd=str(self.audio.parent), env=env, creationflags=flags,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, daemon=True).start()

    def stop(self):
        """Zatrzymuje z zapisem stanu (fw-dd zapyta: zapisać stan? -> t, przenieść? -> n)."""
        if not self.proc or self.proc.poll() is not None:
            return
        self._stopping = True
        try:
            self.proc.stdin.write(b"t\nn\n")
            self.proc.stdin.flush()
        except OSError:
            pass
        self._flag.touch()
        threading.Timer(90, self._kill_if_alive).start()

    def _kill_if_alive(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _read(self):
        buf = b""
        stream = self.proc.stdout
        while True:
            ch = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                self._handle(raw.decode("utf-8", "replace").strip())
        if buf:
            self._handle(buf.decode("utf-8", "replace").strip())
        code = self.proc.wait()
        self._flag.unlink(missing_ok=True)
        self._fix_nested()
        if self._stopping:
            status = "stopped"
        elif code == 0 and result_txt(self.audio):
            status = "done"
        else:
            status = "error"
        self.on_done(status, result_txt(self.audio))

    def _fix_nested(self):
        """fw-dd.py przenosi wynik do <folder>/przetworzone. Jeśli plik już leżał w 'przetworzone',
        powstałoby przetworzone/przetworzone – przenosimy pliki z powrotem poziom wyżej."""
        if self.audio.parent.name != "przetworzone":
            return
        nested = self.audio.parent / "przetworzone"
        if not nested.is_dir():
            return
        for ext in (self.audio.suffix, ".txt", ".srt", ".vtt"):
            f = nested / (self.audio.stem + ext)
            if f.exists():
                try:
                    f.replace(self.audio.parent / f.name)
                except OSError:
                    pass
        try:
            nested.rmdir()  # tylko jeśli pusty
        except OSError:
            pass

    def _handle(self, line):
        if not line:
            return
        m = _PCT.search(line)
        if m:
            self.on_progress(float(m.group(1)) / 100)
            return
        self.log.append(line)
        del self.log[:-400]
        seg = _SEG.match(line)
        self.on_line(seg.group(1) if seg else line)
