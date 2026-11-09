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

## Nagrywanie

Mikrofon i dźwięk z głośników (rozmówcy) są nagrywane do **dwóch osobnych kanałów**. Po zakończeniu nagrania
każdy kanał jest obrabiany oddzielnie: ma własny profil szumu i własne wyrównanie głośności. Potem oba
łączą się w jeden plik Opus mono, który zastępuje surowy. Dzięki temu cichy rozmówca nie ginie pod głośnym,
także gdy mówicie jednocześnie. Szum z Twojego mikrofonu jest usuwany niezależnie od dźwięku rozmówców.
Obróbkę (wyrównanie, szum) i zachowanie surowego nagrania ustawiasz w Nagrywanie → **⚙ Opcje**.

Obrobione pliki mają w metadanych znacznik (co zrobiono i kiedy). Transkrypcja i konwersja wyłączają wtedy
dla tego pliku kroki, które już były, i pokazują to w jego wierszu. Dzięki temu nic nie jest obrabiane dwa razy.

## Ustawienia

- **⚙ na samej górze**: ustawienia całej aplikacji (wygląd).
- **📌 obok ⚙**: okno zawsze na wierzchu (włącz albo wyłącz jednym kliknięciem; aplikacja to zapamiętuje).
- **⚙ Opcje w każdej zakładce**: ustawienia danego tematu. Pod listą widać ich podsumowanie, a kliknięcie je otwiera.
  - Nagrywanie: folder nagrań, jakość (24/32/48 kbps), obróbka po nagraniu, zachowanie surowego nagrania.
  - Transkrypcja: model, urządzenie, rdzenie, format, monitor folderu.
  - Konwersja: wszystko poniżej.

## Konwersja

Zamienia nagrania (np. MP4 z OBS) na małe pliki Opus. Działa w osobnym procesie o niskim priorytecie.
W **⚙ Opcje** zakładki ustawiasz:

- **Jakość pliku** (24/32/48 kbps), niezależnie od jakości nagrywania.
- **Wyrównanie głośności** i **usuwanie szumu**.
- **Rdzenie CPU**: **Automatycznie** (domyślnie) to rdzenie E podczas nagrywania i P poza nim, z przełączaniem
  w trakcie pliku. Można też wybrać na stałe **P**, **E** albo **Wszystkie**.
- **Oryginał po konwersji**: zostaw, przenieś **do Kosza** (da się przywrócić) albo usuń na stałe.
  Przed usunięciem aplikacja sprawdza, czy długość nagrania się zgadza.
- **Zapis wyniku**: obok oryginału albo do wybranego folderu. Istniejące pliki nie są nadpisywane,
  tylko dostają dopisek (2), (3)…

Konwersja używa jednego wątku. Na i5-1345U nagranie 41 min z wyrównaniem i odszumianiem trwało ok. 1,5 min na P
i ok. 3 min na E. „Wszystkie” nie przyspiesza. Rdzenie, wyrównanie i odszumianie dotyczą też przygotowania
dźwięku przed transkrypcją.

## Prywatność

Nagrania, transkrypcje, `config.json` i logi są w `.gitignore` i nie trafiają do repo.
Mikrofon jest otwierany tylko podczas nagrywania albo na 10 s po kliknięciu
„Sprawdź poziomy”.
