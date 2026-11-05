"""
worker.py – osobny proces transkrypcji (uruchamiany przez transcriber.py).

Dlaczego osobny proces: przypięcie do rdzeni (np. tylko E) i niski priorytet dotyczą
wtedy tylko transkrypcji, a nie okna i nagrywania; pamięć po modelu (1–3 GB)
zwalnia się po zakończeniu kolejki; awaria sterownika grafiki nie zamyka aplikacji.

Protokół: JSON, jedna wiadomość na linię.
  stdin  ← {"cmd": "load", "opts": {...}}        załaduj model
           {"cmd": "job", "audio": "...", "opts": {...}}
           (przerwanie bieżącego pliku z zapisem stanu = utworzenie pliku-flagi z argv[1])
           {"cmd": "quit"}
  stdout → {"ev": "loaded", "device": ...} | {"ev": "log", "msg": ...}
           {"ev": "segment", "start", "end", "text"} | {"ev": "progress", "f": 0..1}
           {"ev": "done", ...wynik transcribe_file} | {"ev": "error", "msg", "trace"}
Wszystko, co biblioteki wypisują same (pobieranie modelu itp.), idzie na stderr.
"""

import json
import os
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Kanał protokołu = kopia oryginalnego stdout; zwykłe print()/C-printf trafiają na stderr.
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
_lock = threading.Lock()


def emit(ev, **data):
    with _lock:
        _proto.write(json.dumps({"ev": ev, **data}, ensure_ascii=False) + "\n")
        _proto.flush()


def log(msg):
    emit("log", msg=str(msg))


def main():
    import engine

    # UWAGA (Windows): stdin czytamy WYŁĄCZNIE między zadaniami, w jednym wątku.
    # Wątek czekający na stdin w tle blokuje m.in. "import openvino" (zawieszenie na stałe).
    # Dlatego przerwanie bieżącego pliku = pojawienie się pliku-flagi (argv[1]).
    flag = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    should_stop = (lambda: flag.exists()) if flag else (lambda: False)
    lm = None
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd = msg.get("cmd")
        if cmd == "quit":
            break
        try:
            if cmd == "load":
                log(f"Ładowanie modelu {msg['opts']['model']}…")
                lm = engine.load_model(msg["opts"], log=log)
                emit("loaded", device=lm.device)
            elif cmd == "job":
                if flag:
                    flag.unlink(missing_ok=True)
                res = engine.transcribe_file(
                    lm, msg["audio"], msg["opts"],
                    on_segment=lambda s: emit("segment", start=s.start, end=s.end, text=s.text.strip()),
                    on_progress=lambda f: emit("progress", f=f),
                    should_stop=should_stop, log=log)
                emit("done", **res)
        except Exception as e:
            emit("error", msg=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
        finally:
            if flag:
                flag.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
