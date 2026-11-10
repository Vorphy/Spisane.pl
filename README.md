# Spisane.pl – Notatnik spotkań

Aplikacja na Windows do nagrywania rozmów (mikrofon + dźwięk z głośników, WASAPI loopback)
do plików Opus, konwersji starych nagrań i kolejki transkrypcji.

**Pełny podręcznik użytkownika:** [`podrecznik.html`](podrecznik.html). Opisuje każdą opcję, działanie silników
i rdzeni procesora, pomiary i rozwiązywanie problemów. W aplikacji otwiera go przycisk **?** w nagłówku,
klawisz **F1**, „Podręcznik” w menu ikony w zasobniku albo **?** w każdym oknie Opcji (od właściwego rozdziału).

## Uruchomienie na nowym komputerze

1. Sklonuj repo albo pobierz ZIP.
2. Kliknij dwukrotnie `uruchom.bat`.
   - Jeśli nie ma Pythona, skrypt zaproponuje instalację Python 3.12 przez `winget`.
   - Przy pierwszym starcie aplikacja sprawdzi pakiety (`requirements.txt`),
     pokaże, czego brakuje, i po Twojej zgodzie doinstaluje je przez pip.

## Transkrypcja

Wbudowana (Whisper large-v3-turbo), dawny skrypt `fw-dd.py` jest teraz częścią aplikacji:

- **Automatycznie** (domyślnie): aplikacja sama wybiera najlepsze urządzenie dla tego komputera.
  Grafika Intel: OpenVINO. Inna grafika (AMD Radeon, NVIDIA): Vulkan. Brak grafiki: procesor.
- **Grafika Intel** (OpenVINO): ok. 6× szybciej, niż trwa nagranie.
  Fragmenty pominięte przez model wykrywa VAD i transkrybuje ponownie.
- **Grafika Vulkan** (whisper.cpp): działa na każdej grafice z Vulkanem, np. AMD Radeon w Ryzenach.
  Program (budowany z oficjalnych źródeł przez workflow w tym repo, ze sprawdzaną sumą kontrolną)
  i model (ok. 0,9 GB) pobierają się przy pierwszym użyciu.
- **Procesor** (faster-whisper): wątki, rdzenie P/E/wszystkie/N, beam size, typ obliczeń.
- Gdy wybrana grafika nie działa, transkrypcja sama przechodzi na procesor.

Test na nagraniu z ręczną transkrypcją (5,8 min, Intel Iris Xe, ze słownikiem nazw):

| Urządzenie | Błędne słowa (WER) | Błędne litery (CER) | Czas |
|---|---|---|---|
| Grafika Intel (OpenVINO) | 22,6% | 12,6% | 59 s |
| Grafika Vulkan (whisper.cpp, beam 2) | 24,1% | 12,2% | 65 s |
| Procesor (beam 2, rdzenie P) | 24,4% | 13,4% | 184 s |

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
Wykrywanie mowy (Silero VAD z faster-whisper) wycisza Twój mikrofon, gdy nie mówisz. Dzięki temu
oddechy, siorbanie czy stukanie nie nakładają się na rozmówców (przełącznik „wycisz mój mikrofon, gdy nie mówię”).
Obróbkę (wyrównanie, szum) i zachowanie surowego nagrania ustawiasz w Nagrywanie → **⚙ Opcje**.

Obrobione pliki mają w metadanych znacznik (co zrobiono i kiedy). Transkrypcja i konwersja wyłączają wtedy
dla tego pliku kroki, które już były, i pokazują to w jego wierszu. Dzięki temu nic nie jest obrabiane dwa razy.

## Ustawienia

- **⚙ na samej górze**: ustawienia całej aplikacji:
  - wygląd,
  - **zasobnik**: minimalizacja chowa okno do ikony obok zegara, opcjonalnie także krzyżyk. Kliknięcie ikony
    przywraca okno, a w menu ikony są Nagrywaj/Zatrzymaj i Zakończ. Podczas nagrywania ikona ma czerwoną kropkę,
    a po najechaniu pokazuje czas.
  - **autostart z Windows**: wpis tylko dla Twojego konta, opcjonalnie od razu schowany w zasobniku.
- Aplikacja działa tylko w jednej kopii: ponowne uruchomienie pokazuje okno już działającej.
- Windows nie przyhamowuje aplikacji w tle (tryb wydajności jest dla niej wyłączony), a w trakcie nagrywania
  i obróbki komputer nie przechodzi w uśpienie.
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
- **Rdzenie CPU**: **Automatycznie** (domyślnie): jeden plik idzie na P, kilka naraz na wątki poza zapasem dla
  systemu (u Intela to rdzenie E), a podczas nagrywania na E. Rdzenie przełączają się w trakcie pliku.
  Można też wybrać na stałe **P**, **E** albo **Wszystkie**.
- **Plików naraz**: **Automatycznie** to do 3 plików, podczas nagrywania do 2, na baterii 1.
  Ręcznie można wybrać od 1 do liczby wątków procesora pomniejszonej o zapas dla systemu.
  Zapas wynosi 1 wątek przy ≤ 4, 2 przy ≤ 8 i 4 przy większej liczbie. Każdy plik ma własny proces
  o niskim priorytecie, więc praca na pierwszym planie nie zwalnia.
- **Oryginał po konwersji**: zostaw, przenieś **do Kosza** (da się przywrócić) albo usuń na stałe.
  Przed usunięciem aplikacja sprawdza, czy długość nagrania się zgadza.
- **Zapis wyniku**: obok oryginału albo do wybranego folderu. Istniejące pliki nie są nadpisywane,
  tylko dostają dopisek (2), (3)…

Jeden plik używa jednego wątku. Pomiar na i5-1345U (z zasilacza), nagranie 42,6 min z wyrównaniem
i odszumianiem: 47 s na P i 105 s na E. Trzy pliki (126 min nagrań) naraz trwały 102 s na P i 119 s na E,
a po kolei na P ok. 140 s. Więcej niż 3–4 naraz nie przyspiesza laptopa z limitem mocy, ale może
przyspieszyć komputer stacjonarny. Rdzenie, wyrównanie i odszumianie dotyczą też przygotowania dźwięku
przed transkrypcją.

## Prywatność

Nagrania, transkrypcje, `config.json` i logi są w `.gitignore` i nie trafiają do repo.
Mikrofon jest otwierany tylko podczas nagrywania albo na 10 s po kliknięciu
„Sprawdź poziomy”.
