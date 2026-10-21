# SolMate OS

Steuer- und Überwachungssoftware für den **EET SolMate 3** (Balkon-Solarspeicher).

Sie läuft auf einem Rechner im Heimnetz (z. B. Raspberry Pi, NAS, PC) und spricht über die offizielle
WebSocket-API von EET mit dem Gerät – entweder über den EET-Server oder direkt im lokalen Netz.

> **Hinweis:** Das ist keine Firmware. Die Firmware im SolMate stammt von EET und wird weder ersetzt noch verändert.
> SolMate OS nutzt nur die Einstellungen, die EET über seine API freigibt (dieselben wie in der SolMate-App).
> Inoffizielles Projekt, nicht mit EET verbunden.

## Funktionen

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

## Kommandozeile

```bash
solmate-os -c config.toml status                       # aktuelle Werte als JSON
solmate-os -c config.toml set --min 100 --max 600 --reserve 20
solmate-os -c config.toml boost 30 600                 # 30 Minuten mit 600 W
solmate-os -c config.toml boost 0 0                    # Boost beenden
```

## REST-API

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/status` | Live-Werte, aktive Einstellungen, Automatik-Status, Zeitplan |
| GET | `/api/history?hours=24` | Messwerte-Verlauf |
| GET | `/api/energy` | Energiebilanz heute und 7 Tage |
| GET | `/api/events` | Ereignisprotokoll |
| POST | `/api/automatic` | `{"enabled": true}` |
| POST | `/api/settings` | `{"min_w": 100, "max_w": 600, "reserve_percent": 20, "minutes": 60}` |
| POST | `/api/boost` | `{"watts": 600, "minutes": 30}` (0 Minuten = beenden) |

Die API hat keine Anmeldung. Betreiben Sie sie nur im eigenen Heimnetz (`host = "127.0.0.1"` beschränkt sie auf den Rechner selbst).

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
| `solmate_os/controller.py` | Regelautomatik und Entscheidungslogik |
| `solmate_os/storage.py` | SQLite-Speicher, Verlauf, Energiebilanz |
| `solmate_os/web.py`, `static/index.html` | REST-API und Weboberfläche |
| `solmate_os/cli.py` | Kommandozeile |

## Entwicklung

```bash
pip install -e '.[dev]'
pytest
```

Die Tests prüfen den Client gegen einen nachgebauten EET-Server (Anmeldung, Token, Weiterleitung, Verbindungsabbruch),
die Zeitplan- und Schutzlogik, den Simulator und die REST-API.

## Grenzen

- Die Bedeutung einiger Messfelder (`battery_flow`, `battery_state`) ist aus dem offiziellen SDK und der
  Community abgeleitet; EET dokumentiert sie nicht vollständig. Unbekannte Felder werden unter `raw` mitgeliefert.
- Ob ein Wert übernommen wird, entscheidet am Ende die Firmware des SolMate (z. B. bei schwacher Batterie).
- Am echten Gerät noch nicht getestet – bitte beim ersten Einsatz die Werte mit der SolMate-App vergleichen.

## Lizenz

MIT. Das Protokoll folgt dem offiziellen [solmate-sdk](https://github.com/eet-energy/solmate-sdk) von EET (MIT).
