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

Zakładka Transkrypcja uruchamia zewnętrzny skrypt `fw-dd.py` (nie ma go w repo).
Skopiuj go obok aplikacji albo do folderu wyżej, lub wskaż ścieżkę w Ustawieniach.

## Prywatność

Nagrania, transkrypcje, `config.json` i logi są w `.gitignore` i nie trafiają do repo.
Mikrofon jest otwierany tylko podczas nagrywania albo na 10 s po kliknięciu
„Sprawdź poziomy”.
