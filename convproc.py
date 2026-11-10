"""
convproc.py – konwersja w osobnym procesie (convert_worker.py), z niskim priorytetem
i przypięciem do wybranych rdzeni. Ten sam interfejs co converter.convert_to_opus, plus:

    dst = convert(src, dst=None, bitrate=..., level=..., denoise=...,
                  cores="auto", recording=lambda: bool, on_cores=lambda kind, desc: ...,
                  progress=lambda f: ..., cancel=lambda: bool)

cores: "auto" | "p" | "e" | "all" | N.
  "auto" = rdzenie E, gdy trwa nagrywanie (recording() -> True), w przeciwnym razie P.
  Sprawdzane co pół sekundy – przy zmianie proces jest przepinany w locie.
  (Pomiar 2026-11-09, i5-1345U: konwersja jest jednowątkowa; na P ok. 2–2,4× szybciej niż na E,
   "wszystkie" nie przyspiesza – Windows przerzuca ją tylko między P i E.)
cancel() -> True zabija proces i rzuca InterruptedError (jak converter).
"""

import json
import os
import subprocess
import threading
from pathlib import Path

import engine
from transcriber import python_console_exe

WORKER = Path(__file__).resolve().parent / "convert_worker.py"
CHECK_EVERY = 0.5   # s – jak często sprawdzać przerwanie i stan nagrywania


class ConvertError(RuntimeError):
    """Błąd w procesie konwersji; .detail = ślad z procesu (do logu)."""
    def __init__(self, msg, detail=""):
        super().__init__(msg)
        self.detail = detail


def resolve_cores(cores, recording=None):
    if str(cores) == "auto":
        return "e" if (recording and recording()) else "p"
    return str(cores)


def convert(src, dst=None, bitrate=32000, level=False, denoise=False, cores="auto",
            recording=None, on_cores=None, progress=None, cancel=None, source="konwersja",
            split_channels=False, gate=False):
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".opus")
    args = {"src": str(src), "dst": str(dst), "bitrate": bitrate,
            "level": bool(level), "denoise": bool(denoise), "source": source,
            "split_channels": bool(split_channels), "gate": bool(gate)}
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([python_console_exe(), "-u", str(WORKER), json.dumps(args)],
                            env=env, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    engine.no_power_throttling(getattr(proc, "_handle", None))
    err_tail = []

    def read_err():
        for raw in proc.stderr:
            err_tail.append(raw.decode("utf-8", "replace").rstrip())
            del err_tail[:-50]
    threading.Thread(target=read_err, daemon=True).start()

    stopped = threading.Event()
    current = [None]

    def pin():
        kind = resolve_cores(cores, recording)
        if kind != current[0]:
            current[0] = kind
            desc = engine.set_affinity(kind, handle=getattr(proc, "_handle", None)) if os.name == "nt" else kind
            if on_cores:
                on_cores(kind, desc)

    pin()   # od razu po starcie – proces dopiero ładuje biblioteki

    def watch():
        while proc.poll() is None:
            if cancel and cancel():
                stopped.set()
                proc.kill()
                return
            try:
                pin()
            except Exception:
                pass
            stopped.wait(CHECK_EVERY)
    threading.Thread(target=watch, daemon=True).start()

    result = None
    try:
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("ev") == "progress" and progress:
                progress(ev["f"])
            elif ev.get("ev") in ("done", "error"):
                result = ev
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if stopped.is_set():
        _cleanup(dst)
        raise InterruptedError("Przerwano")
    if result and result["ev"] == "done":
        return Path(result["dst"])
    _cleanup(dst)
    if result:
        raise ConvertError(result["msg"], result.get("trace", ""))
    raise ConvertError(f"Proces konwersji zakończył się kodem {code}", "\n".join(err_tail))


def _cleanup(dst):
    part = dst.with_name(dst.name + ".part")
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
