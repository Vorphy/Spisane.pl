"""
convert_worker.py – osobny proces konwersji (uruchamiany przez convproc.py).

Dlaczego osobny proces: przypięcie do rdzeni i niski priorytet dotyczą wtedy tylko
konwersji, a nie okna i nagrywania (te są w procesie aplikacji).

Wywołanie: python convert_worker.py '<json z parametrami>'
  {"src", "dst", "bitrate", "level", "denoise"}
Rdzenie ustawia z zewnątrz convproc.py (także w locie, np. gdy zacznie się nagrywanie).
stdout → jedna wiadomość JSON na linię:
  {"ev": "progress", "f"} | {"ev": "done", "dst"} | {"ev": "error", "msg", "trace"}
Przerwanie = zabicie procesu (niepełny plik .part sprząta convproc.py).
"""

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Kanał protokołu = kopia oryginalnego stdout; wszystko inne (biblioteki) idzie na stderr.
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def emit(ev, **data):
    _proto.write(json.dumps({"ev": ev, **data}, ensure_ascii=False) + "\n")
    _proto.flush()


def main():
    try:
        a = json.loads(sys.argv[1])
        import converter
        last = [-1.0]

        def progress(f):
            if f - last[0] >= 0.002 or f >= 1:      # bez zalewania kanału komunikatami
                last[0] = f
                emit("progress", f=f)

        dst = converter.convert_to_opus(a["src"], a.get("dst"), bitrate=a.get("bitrate", 32000),
                                        level=a.get("level", False), denoise=a.get("denoise", False),
                                        progress=progress)
        emit("done", dst=str(dst))
    except Exception as e:
        emit("error", msg=f"{type(e).__name__}: {e}", trace=traceback.format_exc())


if __name__ == "__main__":
    main()
