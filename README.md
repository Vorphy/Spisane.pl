# Spisane.pl – Notatnik spotkań

Aplikacja na Windows do nagrywania rozmów (mikrofon + dźwięk z głośników, WASAPI loopback)
do plików Opus, konwersji starych nagrań i kolejki transkrypcji.

## Uruchomienie na nowym komputerze

1. Sklonuj repo albo pobierz ZIP.
2. Kliknij dwukrotnie `uruchom.bat`.
   - Jeśli nie ma Pythona, skrypt zaproponuje instalację Python 3.12 przez `winget`.
   - Przy pierwszym starcie aplikacja sprawdzi pakiety (`requirements.txt`),
     pokaże, czego brakuje, i po Twojej zgodzie doinstaluje je przez pip.

## Transkrypcja

Wbudowana (Whisper large-v3-turbo), dawny skrypt `fw-dd.py` jest teraz częścią aplikacji:

- **Grafika Intel Iris Xe** (OpenVINO, domyślnie), ok. 5–7× szybciej, niż trwa nagranie.
  Fragmenty pominięte przez model wykrywa VAD i transkrybuje ponownie.
  Bez grafiki Intel aplikacja sama przechodzi na procesor.
- **Procesor** (faster-whisper): wątki, rdzenie P/E/wszystkie/N, beam size, typ obliczeń.
- Formaty txt (z czasem albo bez), srt, vtt albo wszystkie naraz; filtr VAD; przycinanie do zadanego czasu.
- **Zatrzymaj** zapisuje stan, a **Transkrybuj** wznawia od tego miejsca, także po ponownym uruchomieniu aplikacji.
- **Monitor folderu**: nowe nagrania same trafiają do kolejki, gdy skończy się ich zapis
  (np. z OBS albo z zakładki Nagrywanie).
- Gotowe pliki i wyniki trafiają do podfolderu `przetworzone`.

Wszystkie opcje są w zakładce Transkrypcja → **⚙ Opcje**. Transkrypcja działa w osobnym procesie
o niskim priorytecie, więc okno i nagrywanie nie zwalniają, a pamięć po modelu zwalnia się po kolejce.
Pierwsza transkrypcja pobiera model (ok. 1–3 GB).

## Prywatność

Nagrania, transkrypcje, `config.json` i logi są w `.gitignore` i nie trafiają do repo.
Mikrofon jest otwierany tylko podczas nagrywania albo na 10 s po kliknięciu
„Sprawdź poziomy”.
