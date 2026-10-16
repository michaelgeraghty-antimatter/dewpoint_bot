#!/usr/bin/env bash
# Install the dewpoint bot on a site VM (Debian/Ubuntu or any systemd Linux).
#
#   sudo ./install.sh            first run: writes /etc/dewpoint-bot.env for you to fill in
#   sudo ./install.sh            second run: installs, dry-runs, starts the hourly timer
#   sudo ./install.sh --test     same, plus posts a setup-check message to each channel
#   sudo ./install.sh --uninstall
#
# Re-running is safe; it's also how you upgrade dewpoint_bot.py.
set -euo pipefail

APP_DIR=/opt/dewpoint-bot
ENV_FILE=/etc/dewpoint-bot.env
UNIT_DIR=/etc/systemd/system
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl disable --now dewpoint-bot.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/dewpoint-bot.service" "$UNIT_DIR/dewpoint-bot.timer"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  echo "Removed. Left $ENV_FILE and /var/lib/dewpoint-bot in place; delete them by hand if wanted."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 not found: apt-get install -y python3" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' \
  || { echo "python3 3.7+ required" >&2; exit 1; }

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# Dewpoint bot config for this site.
# Keep this file private (it is chmod 600): anyone with a webhook URL can post to that channel.
# Quote values that contain spaces.

SITE_NAME="Site name here"
ZIP_CODE=""
# One incoming-webhook URL per channel, comma-separated for more than one channel.
SLACK_WEBHOOK_URL=""

# Optional overrides (defaults shown)
#COUNTRY="us"
#SPREAD_F="5"
#HOURS="24"
#REALERT_HOURS="12"
#SEND_ALL_CLEAR="1"
EOF
  echo "Created $ENV_FILE. Fill in SITE_NAME, ZIP_CODE and SLACK_WEBHOOK_URL, then re-run: sudo $0"
  exit 0
fi

# Load the config to validate it and for the dry run.
set -a; . "$ENV_FILE"; set +a
[[ -n "${ZIP_CODE:-}" && -n "${SLACK_WEBHOOK_URL:-}" ]] \
  || { echo "Set ZIP_CODE and SLACK_WEBHOOK_URL in $ENV_FILE first." >&2; exit 1; }
chmod 600 "$ENV_FILE"

install -d -m 755 "$APP_DIR"
install -m 755 "$SRC_DIR/dewpoint_bot.py" "$APP_DIR/dewpoint_bot.py"

cat > "$UNIT_DIR/dewpoint-bot.service" <<EOF
[Unit]
Description=Dewpoint forecast check and Slack alert
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
# Runs as a throwaway unprivileged user; state survives in /var/lib/dewpoint-bot.
DynamicUser=yes
StateDirectory=dewpoint-bot
Environment=STATE_FILE=/var/lib/dewpoint-bot/state.json
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/env python3 $APP_DIR/dewpoint_bot.py
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
EOF

cat > "$UNIT_DIR/dewpoint-bot.timer" <<'EOF'
[Unit]
Description=Run the dewpoint check hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=5min
# Catch up with one run after the VM was off or asleep.
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "== Dry run (nothing is posted) =="
python3 "$APP_DIR/dewpoint_bot.py" --dry-run

if [[ "${1:-}" == "--test" ]]; then
  echo "== Posting setup-check message to each channel =="
  python3 "$APP_DIR/dewpoint_bot.py" --test
fi

systemctl daemon-reload
systemctl enable --now dewpoint-bot.timer
echo
echo "Installed. Next run: $(systemctl show dewpoint-bot.timer -p NextElapseUSecRealtime --value)"
echo "Logs:      journalctl -u dewpoint-bot.service"
echo "Run now:   sudo systemctl start dewpoint-bot.service"
