"""
transcriber.py – klient procesu transkrypcji (worker.py).

    w = Worker(on_event)          # on_event(ev: dict) – wołane z wątku czytającego
    w.start(opts)                 # uruchamia proces i ładuje model -> {"ev": "loaded"}
    w.job(audio, opts)            # -> "segment"/"progress"/"log" ... "done" albo "error"
    w.stop()                      # przerywa bieżący plik z zapisem stanu -> "done" ze status="stopped"
    w.quit()                      # zamyka proces (zwalnia pamięć po modelu)
Gdy proces zakończy się sam (awaria), przychodzi {"ev": "exit", "code": N}.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from engine import AUDIO_EXTENSIONS, MODEL_KEYS, scan_folder, watch_candidates  # noqa: F401

WORKER = Path(__file__).resolve().parent / "worker.py"


def python_console_exe():
    """python.exe (nie pythonw.exe) – potrzebny do procesu z przechwytywanym wyjściem."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def model_key(opts):
    return tuple(str(opts[k]) for k in MODEL_KEYS)


class Worker:
    def __init__(self, on_event):
        self.on_event = on_event
        self.proc = None
        self.key = None
        self.log = []          # ostatnie linie stderr + komunikaty "log" (do notatnik.log przy błędzie)
        self.flag = Path(tempfile.gettempdir()) / f"notatnik_stop_{os.getpid()}_{id(self)}.flag"

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, opts):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   HF_HUB_DISABLE_PROGRESS_BARS="0")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        self.proc = subprocess.Popen(
            [python_console_exe(), "-u", str(WORKER), str(self.flag)], env=env, creationflags=flags,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.key = model_key(opts)
        threading.Thread(target=self._read_proto, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()
        self._send({"cmd": "load", "opts": opts})

    def job(self, audio, opts):
        self.flag.unlink(missing_ok=True)
        self._send({"cmd": "job", "audio": str(audio), "opts": opts})

    def stop(self):
        """Przerwij bieżący plik (zapis stanu). Proces sprawdza flagę po każdym segmencie."""
        try:
            self.flag.touch()
        except OSError:
            pass

    def quit(self):
        if self.alive:
            self._send({"cmd": "quit"})
        self.proc = None
        self.key = None

    def kill(self):
        if self.alive:
            self.proc.kill()
        self.proc = None
        self.key = None

    def _send(self, msg):
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, AttributeError, ValueError):
            pass

    def _remember(self, line):
        self.log.append(line)
        del self.log[:-400]

    def _read_proto(self, proc):
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("ev") == "log":
                self._remember(ev["msg"])
            elif ev.get("ev") == "error":
                self._remember(ev.get("trace", ev.get("msg", "")))
            self.on_event(ev)
        code = proc.wait()
        if proc is self.proc or code != 0:
            self.on_event({"ev": "exit", "code": code, "current": proc is self.proc})

    def _read_err(self, proc):
        # pobieranie modelu (tqdm) pisze z \r – dzielimy także po nim
        buf = b""
        while True:
            ch = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(1)
            if not ch:
                break
            buf += ch
            *lines, buf = buf.replace(b"\r", b"\n").split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._remember(line)
                    self.on_event({"ev": "stderr", "line": line})
