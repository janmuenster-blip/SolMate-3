#!/bin/bash
# SolMate OS auf einem (zugeklappten) Mac als Dauerdienst einrichten.
#
#   ./tools/macos/install.sh              einrichten bzw. aktualisieren
#   ./tools/macos/install.sh --uninstall  Dienst entfernen, Ruhezustand wieder normal
#
# Was passiert:
#  1. Python-Umgebung (.venv) im Projektordner anlegen und SolMate OS installieren
#  2. ~/.config/solmate-os/env für Passwörter und Schlüssel anlegen (nur für Sie lesbar)
#  3. Hintergrunddienst (launchd) einrichten: startet beim Hochfahren, auch ohne Anmeldung,
#     und nach einem Absturz neu
#  4. Ruhezustand abschalten, damit der Mac auch zugeklappt weiterläuft (pmset)
# Für Schritt 3 und 4 fragt macOS nach Ihrem Mac-Passwort (sudo).
set -euo pipefail

LABEL="de.solmate-os"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
USER_NAME="$(id -un)"
CONF_DIR="$HOME/.config/solmate-os"
ENV_FILE="$CONF_DIR/env"
RUN_SCRIPT="$CONF_DIR/run.sh"
LOG="$HOME/Library/Logs/solmate-os.log"

if [ "$(uname)" != "Darwin" ]; then
  echo "Dieses Skript ist für macOS. Für Linux/Raspberry Pi siehe README (systemd)." >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  echo "Entferne Dienst …"
  sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
  sudo rm -f "$PLIST"
  sudo pmset -a disablesleep 0
  echo "Fertig. Der Mac schläft beim Zuklappen wieder normal. Daten und config.toml bleiben erhalten."
  exit 0
fi

# --- 1. Python ------------------------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 oder neuer fehlt."
  echo "Bitte von https://www.python.org/downloads/macos/ den "macOS 64-bit universal2 installer" laden,"
  echo "installieren und dieses Skript erneut starten."
  exit 1
fi
echo "Python: $PY ($("$PY" --version))"

cd "$REPO"
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .
echo "SolMate OS installiert."

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "config.toml aus der Vorlage angelegt – bitte anpassen."
fi

# --- 2. Geheimnisse -------------------------------------------------------------------------------
mkdir -p "$CONF_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'ENV'
# Passwörter und Schlüssel für SolMate OS. Nur für Ihren Benutzer lesbar.
# Zeilen ohne # werden beim Start gesetzt. Werte mit Sonderzeichen in 'einfache Anführungszeichen'.
# Nach Änderungen neu starten: sudo launchctl kickstart -k system/de.solmate-os
# SOLMATE_PASSWORD=Passwort-aus-der-SolMate-App
# ECOFLOW_ACCESS_KEY=...
# ECOFLOW_SECRET_KEY=...
# SHELLY_PASSWORD=...
ENV
  echo "Vorlage für Passwörter angelegt: $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

cat > "$RUN_SCRIPT" <<RUN
#!/bin/bash
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a
cd "$REPO"
exec "$REPO/.venv/bin/solmate-os" -c "$REPO/config.toml" run
RUN
chmod 700 "$RUN_SCRIPT"

# --- 3. Hintergrunddienst -------------------------------------------------------------------------
TMP_PLIST="$(mktemp)"
cat > "$TMP_PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$USER_NAME</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$RUN_SCRIPT</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PL
plutil -lint "$TMP_PLIST" >/dev/null
echo "Richte Hintergrunddienst ein (macOS fragt nach Ihrem Passwort) …"
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
sudo cp "$TMP_PLIST" "$PLIST"
rm -f "$TMP_PLIST"
sudo chown root:wheel "$PLIST"
sudo chmod 644 "$PLIST"
sudo launchctl bootstrap system "$PLIST"

# --- 4. Zugeklappt wach bleiben -------------------------------------------------------------------
sudo pmset -a disablesleep 1          # auch zugeklappt nicht schlafen
sudo pmset -a sleep 0                 # kein Ruhezustand nach Zeit
sudo pmset -a displaysleep 2          # Bildschirm darf ausgehen
sudo pmset -a autorestart 1 2>/dev/null || true   # nach Stromausfall starten (nicht jedes Modell)

# --- Ergebnis -------------------------------------------------------------------------------------
sleep 3
NAME="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
IP="${IP:-<IP-des-Mac>}"
PORT="$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' config.toml | head -1)"
PORT="${PORT:-8080}"
echo
if curl -s -o /dev/null "http://127.0.0.1:$PORT/haus"; then
  echo "✅ SolMate OS läuft."
else
  echo "⚠️  Dienst noch nicht erreichbar – Protokoll ansehen: tail -50 $LOG"
fi
echo
echo "Auf dem iPad in Safari öffnen und "Zum Home-Bildschirm" hinzufügen:"
echo "   http://$NAME.local:$PORT/haus"
echo "   (oder http://$IP:$PORT/haus)"
echo
echo "Protokoll:   tail -f $LOG"
echo "Neu starten: sudo launchctl kickstart -k system/$LABEL"
echo "Entfernen:   ./tools/macos/install.sh --uninstall"
