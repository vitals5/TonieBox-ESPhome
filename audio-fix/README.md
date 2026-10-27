# Toniebox: I2S-Startfehler untersuchen und begrenzen

Experimenteller Patch für **ESPHome 2026.10.1**. I2S-Retry-Begrenzung und Diagnose sind im Nutzerlog bestätigt; die neue MP3-Retry-Erweiterung ist am Gerät noch nicht getestet.

## Verifizierte Ausgangslage

- TonieBox-ESPhome: `5890f098722f960e15acfcd23cfe425963e9a087`.
- ESPHome-Basis: Tag `2026.10.1`, Commit `4ea26ed65fefaf6ec9d1347285253801db096ee5`.
- GitHub meldet die Veröffentlichung am 27.10.2026 um 18:33:13 UTC. Der Commit trägt ein davon abweichendes Datum (28.10.); deshalb ist das Release-Datum maßgeblich.
- Nutzerbericht: Geräte-Log ESPHome 2026.8.0, kompiliert am 27.10.2026. Fehlerfolge: PLAYING 19:51:29.936, I2S Starting 19:51:30.364, IDLE 19:51:31.091, PLAYING 19:51:31.265, Mixer Stopped 19:51:31.491, Mixer-Startfehler 19:51:31.626, I2S-Setupfehler 19:51:32.705, danach Wiederholungen alle etwa 40–60 ms.
- Nutzerbericht: MP3 Mono, 44100 Hz, 16 Bit; Puffer 32768/5292/8820 Byte. Frühere Logs meldeten DMA-Allokationsfehler. Mono beseitigt den Fehler laut Nutzer nicht.
- Die Dateien `Eingefügtes Markdown.md` und `toniebox-logs (2).txt` fehlen im gelieferten ZIP. `read_thread` wurde aufgerufen; die Anhänge und die früheren Assistentenantworten sind darin nur als Platzhalter vorhanden. Deshalb beruhen die genannten Logdaten auf dem aktuellen Nutzerauftrag.
- Das gelieferte ZIP enthält zusätzlich Behauptungen über ein Log (3) und einen Gerätetest mit 2026.10.1. Die betreffenden Originaldateien liegen hier nicht vor; diese Behauptungen werden nicht als verifizierte Befunde übernommen.

## Quellen und Hypothesen

Zwischen 2026.8.0 und 2026.10.1 liegen bereits diese tatsächlichen Änderungen:

- [I2S: Start beim Stoppen erhalten, #19027](https://github.com/esphome/esphome/pull/19027)
- [I2S: Zustandsänderungen im Loop, #19089](https://github.com/esphome/esphome/pull/19089)
- [Mixer: Start beim Aufräumen erhalten, #19368](https://github.com/esphome/esphome/pull/19368)
- [DMA-Lockstep im laufenden Betrieb neu synchronisieren, #19319](https://github.com/esphome/esphome/pull/19319)
- [Ähnlich gelagerter DMA-Lockstep-Bericht, #16755](https://github.com/esphome/esphome/issues/16755): anderes Fehlerbild, kein Nachweis derselben Ursache.
- [Release 2026.10.1](https://github.com/esphome/esphome/releases/tag/2026.10.1): dessen Patchnotes enthalten keinen spezifischen Fix für diesen Toniebox-Fehler. Die oben genannten Änderungen stammen aus dem Vergleich mit 2026.8.0.

Die verlinkten GitHub-Quellen wurden geprüft. Heruntergeladene Recherchemetadaten und Build-Logs bleiben lokal und werden nicht veröffentlicht.

**Belegt:** Der unveränderte 2026.10.1-I2S-Task-Fehlerpfad beendet den Task, räumt den Kanal auf, protokolliert den Fehler und lässt erneute `play()`-Aufrufe unmittelbar wieder starten. Dafür gibt es anders als bei Driver-/Task-Erstellungsfehlern keine Wartezeit. Er deaktiviert den Kanal auch nach einem fehlgeschlagenen Setup vor dessen Aktivierung. Das erklärt die Fehlerschleife und die zusätzliche Disable-Meldung, aber noch nicht den ersten Setup-Ausfall.

**Offen:** Speicherknappheit bzw. Fragmentierung und kurzzeitig überlappende Lebensdauern von Decoder-, Mixer- und I2S-Ressourcen. Das Repository beschreibt ausdrücklich fehlendes PSRAM und enge SRAM-Grenzen. Laut Nutzer meldeten ältere Logs DMA-Allokationsfehler. Die hier verfügbaren Logangaben liefern weder DMA-free/largest-Werte noch einen präzisen Setup-Fehler; eine Race Condition ist daher ebenso wenig abschließend bewiesen wie Fragmentierung. Der Disable-Fehler ist hier wahrscheinlich eine Folge des fehlgeschlagenen Setups.

AudioPipeline hält bereits ausstehende URL-/Datei-Anforderungen zurück, solange ein Stop läuft; Mixer verhindert eine zweite Task-Erstellung, solange seine alte Task existiert, und erhält einen Start beim Aufräumen. Diese Schichten wurden untersucht, bleiben im Patch unverändert. Ihre unbegrenzten eigenen Retries werden durch diesen Patch nicht global begrenzt.

## Änderung

- I2S-Start/Stop-Kommandos bleiben bei den vorhandenen Event-Bits. Stop verwirft ältere Startwünsche; ein danach eintreffender Start bleibt für die nächste Lebensdauer erhalten.
- Kein Clear-und-Repost eines Start-Bits beim Aufräumen; das schließt ein zusätzliches Fenster gegenüber einem neueren Stop.
- Stop während eines noch nicht angelegten Tasks kann den Start abbrechen.
- I2S-Aktivierung wird für Standard und SPDIF nachgeführt, einschließlich Resync. Disable nur nach erfolgreichem Enable.
- Kanal und beide Event-Queues werden nach Task-Ende freigegeben; teilweise angelegte Queues werden bei Setup-Fehlern freigegeben. Bei fehlgeschlagener Kanallöschung bleibt das Handle erhalten und verhindert eine zweite Kanalbelegung.
- Bis zu vier aufeinanderfolgende I2S-Startfehler, Wartezeiten 250/500/1000 ms. Danach Fehler verriegeln. Ein explizites Stop/Finish oder ein erfolgreich gestarteter Task setzt den Zähler zurück. Ein Stop, der erst während der Bereinigung eintrifft, wird dabei ebenfalls berücksichtigt. Setup-Retries benötigen keinen weiteren Play-Aufruf des Producers. Ein neuer Track allein muss diese Verriegelung nicht lösen: zuerst Stop senden.
- Bei fehlgeschlagener Kanallöschung bleibt der Fehler verriegelt, damit keine schnelle Bereinigungsschleife entsteht; ein expliziter Stop erlaubt einen weiteren Bereinigungsversuch. Auch Teilfehler der Kanalinitialisierung verwenden diesen geprüften Pfad.
- Diagnose pro Fehler: Fehlerphase, DMA-fähiger interner Heap frei/größter Block und interner Heap frei/größter Block. Die Werte werden im Loop nach der Bereinigung aufgenommen, bilden also nicht zwingend das Allokationstief im Task ab.
- Standard-I2S meldet zusätzlich Silence-/Audio-Source-Allokation, Preload-, Callback- und Enable-Fehler genauer.

Keine Buffervergrößerung und kein pauschales Delay für jeden Trackwechsel. Die Patch-Datei enthält Änderungen an sechs C++-Quell-/Headerdateien (vier I2S, zwei AudioDecoder). Die Komponentenordner enthalten zusätzlich die unveränderten benötigten I2S-/Audio-Dateien aus dem gepinnten Tag.

## Als external_components verwenden

Für den direkten Download beim Build mit ESPHome **2026.10.1** diesen Eintrag an die bestehende `external_components`-Liste anhängen:

```yaml
  - source:
      type: git
      url: https://github.com/vitals5/TonieBox-ESPhome
      ref: audio-start-fix-2026.10.1
      path: audio-fix/components
    components: [i2s_audio, audio]
    refresh: 0s
```

Für einen reproduzierbaren Build kann `ref` auf die vollständige Commit-SHA dieses Branches gesetzt werden. `refresh: 0s` prüft bei jedem Build auf Branch-Updates.

Alternativ lokal verwenden:

ESPHome **2026.10.1** installieren. Die beiden Ordner `audio-fix/components/i2s_audio` und `audio-fix/components/audio` unter denselben relativen Pfaden neben die eigene YAML kopieren. An die **bestehende** Liste `external_components` der TonieESP.yaml einen weiteren Eintrag anhängen:

```yaml
  - source:
      type: local
      path: audio-fix/components
    components: [i2s_audio, audio]
```

Der bestehende Eintrag für trf7962a/dac3100/lis3dh bleibt erforderlich. Keine zweite gleichnamige YAML-Hauptsektion anlegen. Keine anderen ESPHome-Versionen ohne erneuten Kompatibilitätstest verwenden.

Dann im ESPHome-Builder Builddateien bereinigen und neu kompilieren. Erst nach erfolgreichem Build die eigene Firmware installieren. Diese Lieferung enthält keine für dein WLAN vorbereitete Firmware.

`build.yaml` ist eine vollständige Testkonfiguration auf Basis der Originaldatei. Sie erwartet die Toniebox-Komponenten unter `../components` und die gepatchte I2S-Komponente unter `components`. Für lokale Tests `secrets.example.yaml` nach `secrets.yaml` kopieren; die Beispieldatei enthält ausschließlich künstliche Testwerte. Für das echte Gerät eigene Secrets verwenden. Die persönliche Datei wird nicht mitgeliefert.

## Als ESPHome-Fork verwenden

```bash
git clone --branch 2026.10.1 https://github.com/esphome/esphome.git
cd esphome
git apply --check /pfad/zu/audio-fix/esphome-2026.10.1.patch
git apply /pfad/zu/audio-fix/esphome-2026.10.1.patch
python -m pip install -e .
esphome compile /pfad/zur/eigenen/TonieESP.yaml
```

Ein eigener veröffentlichter Fork wurde nicht erstellt. Die Toniebox-Arbeitskopie enthält die lokale externe Komponente; der Patch lässt sich auf einen ESPHome-Fork anwenden.

## Tests und reale Grenzen

```bash
cp audio-fix/secrets.example.yaml audio-fix/secrets.yaml
python3 audio-fix/tests/test_lifecycle.py
python3 audio-fix/tests/test_mp3_retry.py
esphome compile --only-generate audio-fix/build.yaml
esphome compile audio-fix/build.yaml
```

Der Host-Test extrahiert die tatsächlich ausgelieferten C++-Methoden und kompiliert sie mit g++20 gegen deterministische FreeRTOS-/ESP-IDF-Fakes. Er prüft 15 Szenarien: Kommandoreihenfolge, ausstehender Neustart, Enable/Disable, begrenzte Retries samt Zeitabständen/Stop-Reset, verspätetes TASK_STOPPED, kein zweiter Task und Queue-/Kanalbereinigung, Teilallokationen und fehlgeschlagene Kanalbereinigung. Das testet nicht die vollständige Audio-Task, die ESP-IDF-ABI, ISR-Verhalten oder echte Multicore-Interleavings.

**Bestanden:** 15 I2S-Host-Szenarien und neun MP3-Retry-Szenarien, C++-Codegenerierung für die vollständige Toniebox-Konfiguration, Patch-Anwendbarkeit gegen die sechs direkt vom Release-Tag gelesenen Originaldateien und Whitespace-Prüfung. Die Originaldateien wurden zusätzlich bytegenau mit der installierten ESPHome-Version verglichen.

**Firmware-Build versucht, nicht bestanden:** Der erneute Build verwendet ESPHome 2026.10.1, ESP-IDF 5.5.6 und die Xtensa-Werkzeuge. Die CMake-Komponentenerkennung läuft jetzt über den im ZIP dokumentierten alten `psutil`-Fehler hinaus. Sie scheitert am Netzwerkzugriff auf `https://components-file.espressif.com/components/esphome/esp-audio-libs.json`. Der Host ist in der Cloud-Netzwerkfreigabe nicht enthalten. Deshalb weiterhin kein erfolgreicher ESP32-C++-Compile/Link, keine Firmware und keine RAM-/Flash-Größenmessung. Die lokalen Dateien `build-current.log` und `codegen-current.log` dokumentieren diesen Versuch und die abschließende Codegenerierung; sie gehören nicht zum veröffentlichten Quellcode. Keine Hardware angeschlossen, keine Klang-/NFC-Dauertests durchgeführt. Der Patch ist ein prüfbarer Testkandidat.

## Gerätetest und Auswertung

1. Vorher bekannte funktionierende Firmware/YAML bereithalten. Zuerst unverändert 2026.10.1 als Vergleich testen und das Original-Log sichern; dafür liegt in dieser Aufgabe noch kein verifiziertes Log vor.
2. Patch mit eigenen Secrets kompilieren und installieren. Startlog mit Firmwareversion und Buildzeit sichern.
3. Einen bekannten Track, problematische Tracks, Pause/Resume und NFC-Entfernen/Auflegen testen. Anschließend schnelle Wechsel (z. B. 20 Wiederholungen bei 0,2–1 s Abstand) und abwechselnd Medien/Ansage testen.
4. Bei Fehler vier Versuche mit Backoff erwarten; danach explizit Stop und einen neuen Play-Aufruf senden. Keine endlose 40–60-ms-I2S-Setup-Schleife erwarten.
5. Fehlerphase und DMA-/Internal-Werte sammeln. Großes freies Gesamtvolumen bei kleinem größten Block stützt Fragmentierung; geringe Werte in beiden Größen stützen Knappheit. Beides allein beweist noch keine Leckage. Werte vor/nach wiederholten Wechseln vergleichen; zusätzliche Peakmessung im Task ist gegebenenfalls nötig.
6. Wenn Decoderfehler auch ohne I2S-Setupfehler bleiben, diesen Pfad separat mit Stream-URL/Format und Decoderdiagnose untersuchen. Nicht alle Trackfehler dieser I2S-Ursache zuordnen.

Rückbau: den Eintrag für die gepatchten Komponenten entfernen, Builddateien bereinigen, mit unverändertem ESPHome 2026.10.1 neu bauen und die eigene vorherige Konfiguration installieren.

## Fortsetzung dieser Coding-Aufgabe

Gegenüber dem gelieferten ZIP wurden verspätete Stop-Kommandos im Setup-Fehlerpfad, selbständige Setup-Retries, geprüfte Teilallokations-Bereinigung und das Verriegeln fehlgeschlagener Kanalbereinigung ergänzt. Sieben weitere Host-Szenarien decken diese Fälle ab. Die YAML-Puffer und das Nutzerverhalten bleiben unverändert. Ungeprüfte Log-(3)-Aussagen wurden aus den Befunden entfernt.

Die Retry-Grenze gilt für aufeinanderfolgende **I2S-Startfehler**, nicht für wiederkehrende Fehler nach erfolgreichem Start, Decoderfehler oder die eigenen Mixer-Retries. Eine globale Retry-Steuerung aller Audiostufen ist nicht Bestandteil dieses minimalen Patches. Es wird nicht behauptet, die ursprüngliche Ursache am Gerät nachgewiesen oder behoben zu haben.

## MP3-Erweiterung nach Log (4) und (5)

Die beiden inzwischen bereitgestellten Nutzerlogs bestätigen den I2S-Patch. Log (4) zeigt einen fehlgeschlagenen 8820-Byte-Speaker-Ringpuffer bei einem nach Bereinigung gemeldeten größten internen Block von 7680 Byte. Log (5) bestätigt den auf 60 ms reduzierten I2S-Puffer (5292 Byte); im kurzen Ausschnitt treten keine I2S-Setupfehler auf. Beide Logs enthalten MP3-Decoderfehler `-2`. Die Original-Logs bleiben lokal.

`-2` ist in [micro-mp3 v0.4.0](https://github.com/esphome-libs/micro-mp3/blob/v0.4.0/include/micro_mp3/mp3_decoder.h) `MP3_ALLOCATION_FAILED`. Dessen [Initialisierung](https://github.com/esphome-libs/micro-mp3/blob/v0.4.0/src/mp3_decoder.cpp) läuft vor der Eingabeverarbeitung, verbraucht bei diesem Fehler keine Bytes und gibt Teilallokationen frei. ESPHomes bisherige Pipeline meldet jeden Decoderfehler vor Vorliegen der Streaminformationen als Headerfehler; die Header-Meldung allein beweist deshalb keinen defekten Track.

Die Erweiterung in `audio/audio_decoder.cpp` und `.h` behandelt ausschließlich diesen Allokationsfehler während der MP3-Initialisierung:

- Maximal vier Versuche derselben Initialisierung mit 250/500/1000 ms Abstand. Kein Warten bei erfolgreichem ersten Versuch.
- Derselbe Decoder und dieselben Eingabedaten bleiben erhalten; kein HTTP-Neustart, kein Replay bereits verbrauchter Daten.
- Die Wartezeit verwendet einen gegen `millis()`-Überlauf sicheren Zeitvergleich. Während des Wartens wird `IDLE` zurückgegeben und höchstens 1 ms innerhalb der Retry-Prüfung gewartet. Die bestehende äußere Decoderlogik hat eigene kurze Read-/Write-/Pause-Wartezeiten.
- Die Pipeline erhält zwischen `decode()`-Aufrufen die Kontrolle zurück und prüft weiterhin `PIPELINE_COMMAND_STOP`. Stop/Trackwechsel können so abbrechen. Es entstehen keine zusätzlichen Tasks, Callbacks oder Timer, die nach Ende der Decoderinstanz weiterlaufen.
- Nach vier Fehlern wird der vorhandene Pipeline-Fehlerpfad verwendet; dort kann weiterhin die allgemeine Header-Meldung folgen. Eine neue Decoderinstanz für einen neuen Track hat ein neues Budget.
- Bei Erfolg nach Retry erscheint `MP3 initialization recovered after ...`; bei endgültigem Fehlschlag `MP3 initialization allocation retries exhausted`.
- Vor jedem Initialisierungsversuch (DEBUG) und nach jedem Allokationsfehler (WARN): interner Heap und DMA-fähiger interner Heap, jeweils frei und größter Block.

Die neun Host-Szenarien kompilieren die tatsächlichen `start()`, `decode()` und `decode_mp3_()`-Methoden gegen Codec-/Zeit-/Puffer-Fakes. Sie prüfen Limit/Abstände, Datenerhalt/Wiederanlauf, Timerüberlauf, Reset bei neuem Start, ungültige Eingaben, beschädigte Frames, Fehler nach Streaminitialisierung, Output-Allokationsfehler und kurze Yield-Zeiten. Sie simulieren nicht die vollständige FreeRTOS-Pipeline oder Hardware-Multicore-Interleavings.

Die Erweiterung erzwingt keinen Stop des gemeinsam verwendeten Mixers/I2S-Speakers: Dort kann eine andere Ansage laufen. Sie gibt bestehenden Ressourcen Zeit zum Auslaufen, garantiert aber nicht, dass dadurch genügend Speicher frei wird. Dauerhafter Speichermangel endet nach vier Versuchen. Eine Race Condition ist weiterhin nicht nachgewiesen.

**Wichtig bei bestehender GitHub-Konfiguration:** `components: [i2s_audio]` auf `components: [i2s_audio, audio]` erweitern, `refresh: 0s` verwenden und Builddateien bereinigen. Andernfalls wird die neue Decoder-Erweiterung nicht eingebunden. Den bereits am Gerät getesteten I2S-Puffer von `60ms` in der eigenen YAML beibehalten; die Referenz-Buildkonfiguration verwendet weiterhin die ursprünglichen Pufferwerte, um keine zusätzliche Geräteänderung zu vermischen.
