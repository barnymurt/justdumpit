#!/usr/bin/env bash
# One-shot setup for YTTranscriptScraper on Oracle Cloud Always Free ARM VM.
# Tested on Ubuntu 22.04 LTS / 24.04 LTS.
#
# Usage (as root or with sudo):
#   sudo bash oracle-setup.sh
#
# Environment variables you may want to override:
#   YTSCRAPER_USER     username to run as (default: ytscraper)
#   YTSCRAPER_HOME     install location (default: /opt/ytscraper)
#   YTSCRAPER_DOMAIN   your domain, e.g. justdumpit.online (default: unset -> raw IP)
#   MINIMAX_API_KEY    your MiniMax API key (REQUIRED)

set -euo pipefail

YTSCRAPER_USER="${YTSCRAPER_USER:-ytscraper}"
YTSCRAPER_HOME="${YTSCRAPER_HOME:-/opt/ytscraper}"
YTSCRAPER_DOMAIN="${YTSCRAPER_DOMAIN:-}"
MINIMAX_API_KEY="${MINIMAX_API_KEY:-}"

if [[ -z "$MINIMAX_API_KEY" ]]; then
    echo "ERROR: MINIMAX_API_KEY env var is required." >&2
    echo "  export MINIMAX_API_KEY='your-key'" >&2
    echo "  sudo -E bash oracle-setup.sh" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root. Try: sudo -E bash $0" >&2
    exit 1
fi

echo "==> Creating user $YTSCRAPER_USER"
if ! id "$YTSCRAPER_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --home "$YTSCRAPER_HOME" --create-home "$YTSCRAPER_USER"
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    sqlite3 \
    ca-certificates curl wget \
    caddy

echo "==> Setting up install directory at $YTSCRAPER_HOME"
mkdir -p "$YTSCRAPER_HOME"
chown -R "$YTSCRAPER_USER:$YTSCRAPER_USER" "$YTSCRAPER_HOME"

echo "==> Cloning/pulling project"
if [[ -d "$YTSCRAPER_HOME/.git" ]]; then
    sudo -u "$YTSCRAPER_USER" bash -c "cd '$YTSCRAPER_HOME' && git pull --ff-only"
else
    echo "  No existing repo. Copy project files to $YTSCRAPER_HOME then re-run."
    echo "  rsync -av --exclude='.git' --exclude='__pycache__' ./ $YTSCRAPER_USER@<vm-ip>:$YTSCRAPER_HOME/"
    exit 1
fi

echo "==> Creating Python venv"
sudo -u "$YTSCRAPER_USER" python3.11 -m venv "$YTSCRAPER_HOME/.venv"
sudo -u "$YTSCRAPER_USER" "$YTSCRAPER_HOME/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$YTSCRAPER_USER" "$YTSCRAPER_HOME/.venv/bin/pip" install -r "$YTSCRAPER_HOME/requirements.txt" --quiet

echo "==> Writing .env"
cat > "$YTSCRAPER_HOME/.env" <<EOF
MINIMAX_API_KEY=$MINIMAX_API_KEY
YTSCRAPER_DB_PATH=$YTSCRAPER_HOME/kb.db
EOF
chmod 600 "$YTSCRAPER_HOME/.env"
chown "$YTSCRAPER_USER:$YTSCRAPER_USER" "$YTSCRAPER_HOME/.env"

echo "==> Initialising knowledge base"
sudo -u "$YTSCRAPER_USER" bash -c "cd '$YTSCRAPER_HOME' && .venv/bin/python -m src.cli db"

echo "==> Installing systemd units"
cp "$YTSCRAPER_HOME/scripts/ytscraper-api.service" /etc/systemd/system/
cp "$YTSCRAPER_HOME/scripts/ytscraper-sync.service" /etc/systemd/system/
cp "$YTSCRAPER_HOME/scripts/ytscraper-sync.timer" /etc/systemd/system/
cp "$YTSCRAPER_HOME/scripts/ytscraper-backup.service" /etc/systemd/system/
cp "$YTSCRAPER_HOME/scripts/ytscraper-backup.timer" /etc/systemd/system/
systemctl daemon-reload

systemctl enable --now ytscraper-api.service
systemctl enable --now ytscraper-sync.timer
systemctl enable --now ytscraper-backup.timer

echo "==> Configuring Caddy"
if [[ -n "$YTSCRAPER_DOMAIN" ]]; then
    sed "s/__DOMAIN__/$YTSCRAPER_DOMAIN/g" "$YTSCRAPER_HOME/scripts/Caddyfile" > /etc/caddy/Caddyfile
else
    PUBLIC_IP=$(curl -s ifconfig.me || echo "127.0.0.1")
    cat > /etc/caddy/Caddyfile <<EOF
:80 {
    reverse_proxy 127.0.0.1:8765
}
EOF
    echo "  No domain set -> Caddy will serve on port 80 (HTTP only). Set YTSCRAPER_DOMAIN for HTTPS."
fi
systemctl enable --now caddy
systemctl reload caddy

echo "==> Opening firewall ports (ufw)"
if command -v ufw &>/dev/null; then
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
fi

echo ""
echo "==> Done."
echo "    API:        http$( [[ -n "$YTSCRAPER_DOMAIN" ]] && echo 's' )://${YTSCRAPER_DOMAIN:-<public-ip>}/"
echo "    Logs:       journalctl -u ytscraper-api -f"
echo "    Sync now:   sudo systemctl start ytscraper-sync.service"
echo ""
echo "    Next: add a watched channel:"
echo "      sudo -u $YTSCRAPER_USER -E bash -c 'cd $YTSCRAPER_HOME && .venv/bin/python -m src.cli add-channel https://www.youtube.com/@SomeChannel'"
echo ""