# SolMate OS

Steuer- und Überwachungssoftware für den **EET SolMate 3** (Balkon-Solarspeicher) – mit Web-App fürs iPhone
und Anbindung an das **EcoFlow-STREAM-System** (Stream Ultra/Pro/AC, Smart Meter).

Sie läuft auf einem Rechner im Heimnetz (z. B. Raspberry Pi, NAS, PC) und spricht über die offizielle
WebSocket-API von EET mit dem Gerät – entweder über den EET-Server oder direkt im lokalen Netz.

> **Hinweis:** Das ist keine Firmware. Die Firmware im SolMate stammt von EET und wird weder ersetzt noch verändert.
> SolMate OS nutzt nur die Einstellungen, die EET über seine API freigibt (dieselben wie in der SolMate-App).
> Inoffizielles Projekt, nicht mit EET verbunden.

## Funktionen

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

## Aufbau

| Datei | Inhalt |
|---|---|
| `solmate_os/client.py` | WebSocket-Client (Anmeldung, Token, Weiterleitung, automatischer Neuaufbau) |
| `solmate_os/simulator.py` | Simulierter SolMate mit PV-, Batterie- und Wechselrichtermodell |
| `solmate_os/ecoflow.py` | EcoFlow-API-Client (Signatur, Stream, Smart Meter) und Stream-Simulator |
| `solmate_os/controller.py` | Regelautomatik und Entscheidungslogik |
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
- Am echten Gerät noch nicht getestet – bitte beim ersten Einsatz die Werte mit der SolMate-App vergleichen.

## Lizenz

MIT. Das Protokoll folgt dem offiziellen [solmate-sdk](https://github.com/eet-energy/solmate-sdk) von EET (MIT).
