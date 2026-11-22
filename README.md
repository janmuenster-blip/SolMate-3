# SolMate OS

Steuer- und Überwachungssoftware für den **EET SolMate 3** (Balkon-Solarspeicher) – mit Web-App fürs iPhone
und Anbindung an das **EcoFlow-STREAM-System** (Stream Ultra/Pro/AC, Smart Meter).

Sie läuft auf einem Rechner im Heimnetz (z. B. Raspberry Pi, NAS, PC) und spricht über die offizielle
WebSocket-API von EET mit dem Gerät – entweder über den EET-Server oder direkt im lokalen Netz.

> **Hinweis:** Das ist keine Firmware. Die Firmware im SolMate stammt von EET und wird weder ersetzt noch verändert.
> SolMate OS nutzt nur die Einstellungen, die EET über seine API freigibt (dieselben wie in der SolMate-App).
> Inoffizielles Projekt, nicht mit EET verbunden.

## Funktionen

- **Hausansicht fürs iPad** (`/haus`): Müllabfuhr, Ladestand aller Akkus, PowerStream-Erzeugung mit Prognose,
  EnBW-Stromtarif und Kosten auf einem Bildschirm – siehe [unten](#hausansicht-fürs-ipad)
- **Boost-Button „800 W · 20 min“** ganz oben in der App: ein Tipp, volle Leistung, mit Countdown und Stopp
- **Web-App fürs iPhone:** lässt sich auf den Home-Bildschirm legen, startet im Vollbild wie eine App, optional mit PIN
- **EcoFlow-Stream-Anbindung:** SolMate und Stream teilen sich den Hausverbrauch, ohne gegeneinander zu arbeiten

- **Live-Anzeige:** PV-Leistung, Einspeisung, Batterieladen/-entladen, Ladestand, Temperatur
- **Automatik mit Zeitplan:** Mindest- und Maximal-Einspeisung je Zeitfenster (auch über Mitternacht, nach Wochentagen)
- **Schutzfunktionen:** gesetzliche Einspeisegrenze (Standard 800 W) wird nie überschritten,
  Batteriereserve, Leistungsreduktion bei hoher Temperatur
- **Manuelle Übersteuerung** für eine festgelegte Dauer, danach übernimmt die Automatik wieder
- **Boost:** kurzzeitig feste Leistung einspeisen (z. B. für die Waschmaschine)
- **Datenaufzeichnung** in SQLite mit Verlaufsdiagramm und Energiebilanz (heute / 7 Tage)
- **Weboberfläche** fürs Handy und den PC, funktioniert ohne Internet; Hell- und Dunkelmodus
- **Simulator** zum Ausprobieren ohne Gerät
- **REST-API** für Hausautomation (Home Assistant, ioBroker, Node-RED …)

## Schnellstart

Voraussetzung: Python 3.11 oder neuer.

```bash
pip install .
cp config.example.toml config.toml
solmate-os -c config.toml run
```

Dann im Browser `http://<ip-des-rechners>:8080` öffnen. Ohne Anpassung läuft der **Simulator**.

### Mit dem echten SolMate

In `config.toml`:

```toml
[device]
mode = "cloud"        # oder "local" für direkten Zugriff im Heimnetz
serial = "ABC123..."  # Seriennummer des SolMate
```

Das Passwort ist dasselbe wie in der SolMate-App. Übergeben Sie es per Umgebungsvariable
(`SOLMATE_PASSWORD=... solmate-os -c config.toml run`) oder geben Sie es beim ersten Start ein.
Danach wird nur ein Anmelde-Token gespeichert (`~/.config/solmate-os/tokens.json`), nicht das Passwort.

**Lokaler Modus:** Standardadresse ist `ws://sun2plug.local:9124/`. Falls der Name im Netz nicht auflösbar ist,
die IP des SolMate unter `uri` eintragen.

## Auf dem iPhone nutzen

SolMate OS läuft auf einem Rechner, der dauerhaft an ist (z. B. Raspberry Pi, NAS, Mini-PC). Das iPhone öffnet nur die Oberfläche.

1. In `config.toml` unter `[web]` `host = "0.0.0.0"` setzen (Standard im Beispiel) und am besten eine `pin` vergeben.
2. Dienst starten, die IP-Adresse des Rechners herausfinden (z. B. `hostname -I`).
3. Auf dem iPhone in **Safari** `http://<ip>:8080` öffnen (iPhone muss im selben WLAN sein).
4. Teilen-Symbol → **„Zum Home-Bildschirm“** → Hinzufügen.

Das SolMate-Symbol startet jetzt im Vollbild wie eine normale App. Die PIN wird einmalig in der App eingegeben.

### Von unterwegs (empfohlen: Tailscale)

Den Port **nicht** einfach im Router freigeben. Sicher und kostenlos geht es mit [Tailscale](https://tailscale.com):

1. Tailscale auf dem Rechner (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) und auf dem iPhone (App Store) installieren, beide mit demselben Konto anmelden.
2. Auf dem Rechner: `sudo tailscale serve --bg 8080` – das liefert eine HTTPS-Adresse wie `https://raspberrypi.tailXXXX.ts.net`.
3. Diese Adresse in Safari öffnen und wie oben zum Home-Bildschirm hinzufügen. Das funktioniert zu Hause und unterwegs.

Über HTTPS hält die App zusätzlich ihre Oberfläche offline bereit (Service Worker).

## Hausansicht fürs iPad

Unter `http://<ip>:8080/haus` gibt es eine Übersicht für ein fest aufgestelltes iPad (optimiert für das
iPad Pro 9,7" mit iPadOS 16, quer passt alles ohne Scrollen auf einen Bildschirm, hoch untereinander):

| Kachel | Inhalt | Quelle |
|---|---|---|
| **Müllabfuhr** | Nächster Termin für Gelben Sack, Papier, Restmüll; Banner „Heute Abend rausstellen“ am Vortag | iCal-Link/-Datei der Gemeinde oder feste Rhythmen |
| **Akkus** | Ladestand von SolMate 3, Stream und allen weiteren EcoFlow-Geräten, Laden/Entladen; Solar gesamt, Hausverbrauch, Netz | SolMate-API, EcoFlow-Developer-API, Smart Meter |
| **Solar · PowerStream** | Erzeugung jetzt (PV1/PV2), Abgabe ins Haus, Ertrag heute; Prognose heute/morgen als Kurve mit Ist-Werten; Wetter für 3 Tage | EcoFlow-API, [Open-Meteo](https://open-meteo.com) (kostenlos, ohne Schlüssel) |
| **Shelly** | Aktueller Hausverbrauch je Phase vom Shelly am Zählerkasten; einzelne Verbraucher (Steckdosen) mit Watt und kWh heute | Lokale Shelly-API (Gen1–Gen4) |
| **Strom & Kosten** | Tarif, Netzbezug und Kosten heute/Monat, Ersparnis durch Solarstrom, Hochrechnung Guthaben/Nachzahlung, Zählerstand eintragen, Button zur EnBW-App | `[home.tariff]` + Smart Meter + eingetragene Zählerstände |

**Einrichten** – alles im Abschnitt `[home]` der `config.toml` (Vorlage in `config.example.toml`):

1. **Müllabfuhr:** Auf der Website Ihres Abfallwirtschaftsbetriebs den Abfuhrkalender für Ihre Straße als
   iCal/ICS exportieren bzw. den Abo-Link kopieren und unter `[home.waste] ics_url` eintragen. Einträge werden über
   Stichwörter zugeordnet („gelb“, „wertstoff“ → Gelber Sack; „papier“ → Papier; „rest“ → Restmüll), anpassbar
   unter `[home.waste.types.*]`. Ohne Kalender gehen auch feste Rhythmen (`[[home.waste.rules]]`).
2. **EcoFlow-Geräte:** `solmate-os -c config.toml ecoflow-devices` zeigt alle Seriennummern. Weitere Stream-Einheiten und
   den PowerStream als `[[home.devices]]` eintragen. Prüfen: `solmate-os -c config.toml ecoflow-dump <SN>`.
3. **Shelly (optional):** Jeden Shelly unter `[[home.shelly]]` mit seiner IP eintragen – `role = "grid"` für den
   Zähler am Zählerkasten (Pro 3EM, 3EM, EM), `role = "load"` für einzelne Steckdosen/Verbraucher. Der Shelly muss im
   selben Netz wie der Rechner sein; am besten im Router eine feste IP vergeben. Ein Shelly am Zählerkasten liefert
   Netzbezug und Kosten auch ohne EcoFlow Smart Meter und hat Vorrang vor diesem.
4. **Prognose:** Unter `[home.forecast]` Breiten-/Längengrad und je Modulfläche kWp, Neigung und Ausrichtung eintragen.
5. **EnBW-Tarif:** EnBW bietet Privatkunden **keine offizielle Schnittstelle** an; ein automatischer Login in die EnBW-App
   wäre unzuverlässig und verstößt gegen die Nutzungsbedingungen. Deshalb einmalig aus der EnBW-App bzw. „Mein EnBW“
   übernehmen: Arbeitspreis, Grundpreis, Abschlag, Beginn des Abrechnungsjahres und Zählerstand zu diesem Datum.
   Zählerstände trägt man dann in der Hausansicht ein (gleich wie in der EnBW-App) – daraus entsteht die Hochrechnung.
   Der Button „EnBW-App öffnen“ führt zu „Mein EnBW“ (Ziel über `app_url` änderbar).

**Aufs iPad:** In Safari `http://<ip>:8080/haus` öffnen → Teilen → **„Zum Home-Bildschirm“**. Für den Dauerbetrieb unter
Einstellungen → Anzeige & Helligkeit → Automatische Sperre „Nie“ wählen und das iPad am Netzteil lassen
(ab iPadOS 16.4 hält die Seite den Bildschirm zusätzlich selbst wach). Die Seite aktualisiert sich alle 30 Sekunden.

Im Simulator (Standard) zeigt die Hausansicht Beispielgeräte und eine simulierte Prognose.

## EcoFlow STREAM einbinden

SolMate OS liest über die **offizielle EcoFlow-Developer-API** den Zustand des Stream-Systems und des
EcoFlow Smart Meter und stimmt den SolMate darauf ab.

**Einrichtung**

1. Auf [developer-eu.ecoflow.com](https://developer-eu.ecoflow.com) mit dem EcoFlow-App-Konto anmelden, Entwicklerzugang
   beantragen (die Freischaltung kann einige Tage dauern) und unter „Security“ **Access Key** und **Secret Key** erzeugen.
2. In `config.toml` unter `[ecoflow]` `mode = "cloud"` setzen, Schlüssel per Umgebungsvariable übergeben:
   `ECOFLOW_ACCESS_KEY=… ECOFLOW_SECRET_KEY=…`
3. Seriennummern ermitteln: `solmate-os -c config.toml ecoflow-devices` → `stream_sn` (Haupteinheit) und `meter_sn` eintragen.
4. Prüfen: `solmate-os -c config.toml ecoflow-dump` zeigt Rohdaten und die erkannten Werte. Wird Netzbezug als
   Einspeisung angezeigt, `grid_import_positive = false` setzen.
5. In der EcoFlow-App den Stream im Modus **„Eigenverbrauch“ (Self-powered)** betreiben.

**So arbeiten beide zusammen** (`strategy`)

| Strategie | Verhalten |
|---|---|
| `balance` (Standard) | Der Speicher mit dem volleren Akku führt und deckt den Hausverbrauch; der andere springt nur ein. Wechsel mit 5 % Hysterese. |
| `solmate_first` | SolMate deckt den Verbrauch, der Stream regelt über seinen Smart Meter den Rest. |
| `stream_first` | Stream deckt den Verbrauch, SolMate hilft nur, wenn trotzdem Strom aus dem Netz kommt. |
| `monitor` | Nur anzeigen; die gemeinsame Haushaltsgrenze wird trotzdem eingehalten. |

- **Gemeinsame 800-W-Grenze:** Der SolMate speist höchstens `household_limit_w` minus der aktuellen Stream-Abgabe ein.
- **Boost:** Liefert der Stream gerade Leistung, startet der Boost entsprechend niedriger und steigt auf 800 W,
  sobald der Stream zurückregelt (meist innerhalb von 1–2 Zyklen). Der Hinweis dazu erscheint unter dem Button.
- **Ausfallsicher:** Ist die EcoFlow-API nicht erreichbar, läuft der SolMate mit seinem Zeitplan weiter.
- Zeitpläne bleiben aktiv: ihr Maximum begrenzt den SolMate auch im Zusammenspiel.

## Kommandozeile

```bash
solmate-os -c config.toml status                       # aktuelle Werte als JSON
solmate-os -c config.toml set --min 100 --max 600 --reserve 20
solmate-os -c config.toml boost 30 600                 # 30 Minuten mit 600 W
solmate-os -c config.toml boost 0 0                    # Boost beenden
solmate-os -c config.toml ecoflow-devices              # EcoFlow-Geräte und Seriennummern
solmate-os -c config.toml ecoflow-dump                 # EcoFlow-Rohdaten zur Fehlersuche
```

## REST-API

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/status` | Live-Werte, aktive Einstellungen, Automatik-Status, Zeitplan |
| GET | `/api/history?hours=24` | Messwerte-Verlauf (SolMate und Stream) |
| GET | `/api/energy` | Energiebilanz heute und 7 Tage |
| GET | `/api/events` | Ereignisprotokoll |
| GET | `/api/home` | Hausansicht: Müll, Akkus, Solar + Prognose, Tarif und Kosten |
| POST | `/api/home/meter` | Zählerstand `{"kwh": 14420.5, "day": "2026-11-22"}`; `{"day": …, "delete": true}` löscht |
| POST | `/api/automatic` | `{"enabled": true}` |
| POST | `/api/settings` | `{"min_w": 100, "max_w": 600, "reserve_percent": 20, "minutes": 60}` |
| POST | `/api/boost` | `{}` = Voreinstellung (800 W, 20 min); `{"watts": 600, "minutes": 30}`; 0 Minuten = beenden |

Mit gesetzter `pin` brauchen API-Aufrufe das Sitzungs-Cookie aus `/login`. Ohne PIN nur im eigenen Heimnetz betreiben.

**Tipp – Kurzbefehl in der iPhone-App „Kurzbefehle“:** Aktion „Inhalte von URL abrufen“, URL `http://<ip>:8080/api/boost`,
Methode POST, Anfrage-Text JSON (leer), bei gesetzter PIN zusätzlich Header `X-PIN` mit der PIN.
Dann startet „Hey Siri, Boost“ die 800 W.

## Als Dienst starten (Linux / Raspberry Pi)

`/etc/systemd/system/solmate-os.service`:

```ini
[Unit]
Description=SolMate OS
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/solmate-os
Environment=SOLMATE_PASSWORD=IhrPasswort
ExecStart=/home/pi/.local/bin/solmate-os -c config.toml run
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now solmate-os
```

## Als Dienst auf einem Mac (auch zugeklappt)

Ein altes MacBook eignet sich gut als Dauerrechner. Das Skript `tools/macos/install.sh` richtet alles ein:
Python-Umgebung, Hintergrunddienst (startet beim Hochfahren, auch ohne Anmeldung) und schaltet den Ruhezustand ab,
damit der Mac **zugeklappt** weiterläuft.

1. **Python 3.11+** von [python.org](https://www.python.org/downloads/macos/) installieren
   („macOS 64-bit universal2 installer“), falls `python3 --version` eine ältere Version zeigt.
2. Projekt auf den Mac holen (GitHub → „Code“ → „Download ZIP“, entpacken z. B. nach `~/solmate-os`)
   und die eigene `config.toml` in diesen Ordner legen.
3. Im Terminal:
   ```bash
   cd ~/solmate-os
   ./tools/macos/install.sh
   ```
   macOS fragt einmal nach dem Mac-Passwort. Am Ende steht die Adresse fürs iPad, z. B. `http://MacBook.local:8080/haus`.
4. Passwörter und Schlüssel (SolMate, EcoFlow, Shelly) in `~/.config/solmate-os/env` eintragen
   (die Datei ist nur für Sie lesbar), dann `sudo launchctl kickstart -k system/de.solmate-os`.
5. Mac zuklappen, am Netzteil lassen, fertig.

| Aufgabe | Befehl |
|---|---|
| Protokoll ansehen | `tail -f ~/Library/Logs/solmate-os.log` |
| Neu starten (z. B. nach Änderung der `config.toml`) | `sudo launchctl kickstart -k system/de.solmate-os` |
| Aktualisieren | neue Version entpacken, `./tools/macos/install.sh` erneut ausführen |
| Entfernen (Ruhezustand wieder normal) | `./tools/macos/install.sh --uninstall` |

Hinweise:
- Fragt macOS „Eingehende Verbindungen für Python erlauben?“, mit **Erlauben** bestätigen – sonst erreicht das iPad den Mac nicht.
- Den Mac im WLAN lassen oder per Netzwerkkabel anschließen; im Router am besten eine feste IP vergeben.
- Zugeklappt auf eine harte, freie Fläche legen (nicht aufs Sofa), damit die Wärme abziehen kann.
- Alte Akkus können bei Dauerbetrieb am Netzteil anschwellen: Wölbt sich das Gehäuse oder schließt der Deckel nicht mehr
  bündig, den Mac nicht weiter verwenden. Tools wie „AlDente“ begrenzen die Ladung auf z. B. 80 %.
- Schläft der Mac zugeklappt trotzdem ein (einzelne Modelle/macOS-Versionen), alternativ einen externen Monitor
  anschließen oder ihn aufgeklappt mit dunklem Bildschirm betreiben.

## Aufbau

| Datei | Inhalt |
|---|---|
| `solmate_os/client.py` | WebSocket-Client (Anmeldung, Token, Weiterleitung, automatischer Neuaufbau) |
| `solmate_os/simulator.py` | Simulierter SolMate mit PV-, Batterie- und Wechselrichtermodell |
| `solmate_os/ecoflow.py` | EcoFlow-API-Client (Signatur, Stream, Smart Meter) und Stream-Simulator |
| `solmate_os/controller.py` | Regelautomatik und Entscheidungslogik |
| `solmate_os/home.py` | Hausansicht: fasst alle Quellen zusammen, fragt weitere EcoFlow-Geräte ab |
| `solmate_os/waste.py` | Abfallkalender (ICS mit Wiederholungen, feste Rhythmen) |
| `solmate_os/forecast.py` | PV-Prognose über Open-Meteo |
| `solmate_os/shelly.py` | Shelly-Strommessung über die lokale API (Gen1–Gen4) |
| `solmate_os/tariff.py` | Stromkosten, Ersparnis, Jahreshochrechnung |
| `solmate_os/storage.py` | SQLite-Speicher, Verlauf, Energiebilanz |
| `solmate_os/web.py`, `static/` | REST-API, Web-App, Manifest, Service Worker, Icons |
| `solmate_os/cli.py` | Kommandozeile |

## Entwicklung

```bash
pip install -e '.[dev]'
pytest
```

Die Tests prüfen den Client gegen einen nachgebauten EET-Server (Anmeldung, Token, Weiterleitung, Verbindungsabbruch),
die EcoFlow-Signatur, das Zusammenspiel mit dem Stream (kein Netzbezug, 800-W-Grenze, Boost), die Zeitplan- und
Schutzlogik, PIN-Sperre und die REST-API.

Zum Ausprobieren ohne Geräte: in `config.toml` `[device] mode = "simulator"` und `[ecoflow] mode = "simulator"`.

## Grenzen

- Die Bedeutung einiger Messfelder (`battery_flow`, `battery_state`) ist aus dem offiziellen SDK und der
  Community abgeleitet; EET dokumentiert sie nicht vollständig. Unbekannte Felder werden unter `raw` mitgeliefert.
- Ob ein Wert übernommen wird, entscheidet am Ende die Firmware des SolMate (z. B. bei schwacher Batterie).
- Die EcoFlow-Feldnamen stammen aus der Community-Integration
  [ecoflow-public-api](https://github.com/htims1989/ecoflow-public-api) (Stream Ultra X). Bei anderen Stream-Modellen
  mit `ecoflow-dump` prüfen. Die REST-Abfrage liefert laut EcoFlow teils weniger Felder als MQTT.
- Beim Boost wird Strom, den das Haus gerade nicht braucht, ins Netz eingespeist.
- PowerStream-Werte: Feldnamen und Einheit (0,1 W) laut EcoFlow-Developer-Doku; bei Abweichungen `watts_scale`
  in `[[home.devices]]` setzen. Die Prognose ist eine Schätzung aus Wetterdaten, keine Garantie.
- Kosten ohne EcoFlow Smart Meter: dann wird nur die Ersparnis berechnet, nicht der Netzbezug.
- Am echten Gerät noch nicht getestet – bitte beim ersten Einsatz die Werte mit der SolMate-App vergleichen.

## Lizenz

MIT. Das Protokoll folgt dem offiziellen [solmate-sdk](https://github.com/eet-energy/solmate-sdk) von EET (MIT).
