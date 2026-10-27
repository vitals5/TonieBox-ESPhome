# Toniebox: I2S-Startfehler untersuchen und begrenzen

Experimenteller Patch für **ESPHome 2026.10.1**, kein am Gerät bestätigter Fix.

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

AudioPipeline hält bereits ausstehende URL-/Datei-Anforderungen zurück, solange ein Stop läuft; Mixer verhindert eine zweite Task-Erstellung, solange seine alte Task existiert, und erhält einen Start beim Aufräumen. Diese Schichten wurden untersucht, bleiben im Patch unverändert. Ihre unbegrenzten eigenen Retries werden durch diesen I2S-Patch nicht global begrenzt.

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

Keine Buffervergrößerung und kein pauschales Delay für jeden Trackwechsel. Die Patch-Datei enthält nur Änderungen an vier C++-Dateien. Der Komponentenordner enthält zusätzlich die unveränderten benötigten I2S-Dateien aus dem gepinnten Tag.

## Als external_components verwenden

Für den direkten Download beim Build mit ESPHome **2026.10.1** diesen Eintrag an die bestehende `external_components`-Liste anhängen:

```yaml
  - source:
      type: git
      url: https://github.com/vitals5/TonieBox-ESPhome
      ref: audio-start-fix-2026.10.1
      path: audio-fix/components
    components: [i2s_audio]
    refresh: 0s
```

Für einen reproduzierbaren Build kann `ref` auf die vollständige Commit-SHA dieses Branches gesetzt werden. `refresh: 0s` prüft bei jedem Build auf Branch-Updates.

Alternativ lokal verwenden:

ESPHome **2026.10.1** installieren. Den Ordner `audio-fix/components/i2s_audio` neben die eigene YAML unter `audio-fix/components/i2s_audio` kopieren. An die **bestehende** Liste `external_components` der TonieESP.yaml einen weiteren Eintrag anhängen:

```yaml
  - source:
      type: local
      path: audio-fix/components
    components: [i2s_audio]
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
esphome compile --only-generate audio-fix/build.yaml
esphome compile audio-fix/build.yaml
```

Der Host-Test extrahiert die tatsächlich ausgelieferten C++-Methoden und kompiliert sie mit g++20 gegen deterministische FreeRTOS-/ESP-IDF-Fakes. Er prüft 15 Szenarien: Kommandoreihenfolge, ausstehender Neustart, Enable/Disable, begrenzte Retries samt Zeitabständen/Stop-Reset, verspätetes TASK_STOPPED, kein zweiter Task und Queue-/Kanalbereinigung, Teilallokationen und fehlgeschlagene Kanalbereinigung. Das testet nicht die vollständige Audio-Task, die ESP-IDF-ABI, ISR-Verhalten oder echte Multicore-Interleavings.

**Bestanden:** 15 Host-Szenarien, C++-Codegenerierung für die vollständige Toniebox-Konfiguration, Patch-Anwendbarkeit gegen die vier direkt vom Release-Tag gelesenen Originaldateien und Whitespace-Prüfung. Die Originaldateien wurden zusätzlich bytegenau mit der installierten ESPHome-Version verglichen.

**Firmware-Build versucht, nicht bestanden:** Der erneute Build verwendet ESPHome 2026.10.1, ESP-IDF 5.5.6 und die Xtensa-Werkzeuge. Die CMake-Komponentenerkennung läuft jetzt über den im ZIP dokumentierten alten `psutil`-Fehler hinaus. Sie scheitert am Netzwerkzugriff auf `https://components-file.espressif.com/components/esphome/esp-audio-libs.json`. Der Host ist in der Cloud-Netzwerkfreigabe nicht enthalten. Deshalb weiterhin kein erfolgreicher ESP32-C++-Compile/Link, keine Firmware und keine RAM-/Flash-Größenmessung. Die lokalen Dateien `build-current.log` und `codegen-current.log` dokumentieren diesen Versuch und die abschließende Codegenerierung; sie gehören nicht zum veröffentlichten Quellcode. Keine Hardware angeschlossen, keine Klang-/NFC-Dauertests durchgeführt. Der Patch ist ein prüfbarer Testkandidat.

## Gerätetest und Auswertung

1. Vorher bekannte funktionierende Firmware/YAML bereithalten. Zuerst unverändert 2026.10.1 als Vergleich testen und das Original-Log sichern; dafür liegt in dieser Aufgabe noch kein verifiziertes Log vor.
2. Patch mit eigenen Secrets kompilieren und installieren. Startlog mit Firmwareversion und Buildzeit sichern.
3. Einen bekannten Track, problematische Tracks, Pause/Resume und NFC-Entfernen/Auflegen testen. Anschließend schnelle Wechsel (z. B. 20 Wiederholungen bei 0,2–1 s Abstand) und abwechselnd Medien/Ansage testen.
4. Bei Fehler vier Versuche mit Backoff erwarten; danach explizit Stop und einen neuen Play-Aufruf senden. Keine endlose 40–60-ms-I2S-Setup-Schleife erwarten.
5. Fehlerphase und DMA-/Internal-Werte sammeln. Großes freies Gesamtvolumen bei kleinem größten Block stützt Fragmentierung; geringe Werte in beiden Größen stützen Knappheit. Beides allein beweist noch keine Leckage. Werte vor/nach wiederholten Wechseln vergleichen; zusätzliche Peakmessung im Task ist gegebenenfalls nötig.
6. Wenn Decoderfehler auch ohne I2S-Setupfehler bleiben, diesen Pfad separat mit Stream-URL/Format und Decoderdiagnose untersuchen. Nicht alle Trackfehler dieser I2S-Ursache zuordnen.

Rückbau: lokalen I2S-Eintrag entfernen, Builddateien bereinigen, mit unverändertem ESPHome 2026.10.1 neu bauen und die eigene vorherige Konfiguration installieren.

## Fortsetzung dieser Coding-Aufgabe

Gegenüber dem gelieferten ZIP wurden verspätete Stop-Kommandos im Setup-Fehlerpfad, selbständige Setup-Retries, geprüfte Teilallokations-Bereinigung und das Verriegeln fehlgeschlagener Kanalbereinigung ergänzt. Sieben weitere Host-Szenarien decken diese Fälle ab. Die YAML-Puffer und das Nutzerverhalten bleiben unverändert. Ungeprüfte Log-(3)-Aussagen wurden aus den Befunden entfernt.

Die Retry-Grenze gilt für aufeinanderfolgende **I2S-Startfehler**, nicht für wiederkehrende Fehler nach erfolgreichem Start, Decoderfehler oder die eigenen Mixer-Retries. Eine globale Retry-Steuerung aller Audiostufen ist nicht Bestandteil dieses minimalen Patches. Es wird nicht behauptet, die ursprüngliche Ursache am Gerät nachgewiesen oder behoben zu haben.
