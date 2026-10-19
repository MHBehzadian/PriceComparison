#!/usr/bin/env bash
# Installs PriceComparison as a systemd service.
#   sudo bash deploy/install.sh            (from a checkout)
# Most Iranian shops refuse requests from foreign IPs: install on a server
# inside Iran, or set a proxy in the panel's settings.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/pricecomparison
DATA=/var/lib/pricecomparison
PORT="${PC_PORT:-8090}"
if [ -f /etc/default/pricecomparison ]; then
  PORT="$(sed -n 's/^PC_PORT=//p' /etc/default/pricecomparison | tail -n1)"; PORT="${PORT:-8090}"
fi

if command -v ss >/dev/null && ss -ltnH "( sport = :$PORT )" | grep -q . && ! systemctl is-active --quiet pricecomparison; then
  echo "Port $PORT is already in use. Nothing was changed. Pick another:  PC_PORT=9091 bash deploy/install.sh"
  echo "پورت $PORT اشغال است؛ هیچ تغییری داده نشد. با یک پورت آزاد اجرا کنید."
  exit 1
fi

if ! command -v python3 >/dev/null; then
  apt-get update && apt-get install -y python3 python3-venv
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is required"; exit 1; }

mkdir -p "$DEST" "$DATA"
chmod 700 "$DATA"
if [ "$SRC" != "$DEST" ]; then
  rm -rf "$DEST/pricecomparison"
  cp -r "$SRC/pricecomparison" "$SRC/requirements.txt" "$DEST/"
fi
if ! python3 -m venv "$DEST/venv" >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  apt-get update && { apt-get install -y "python$PYV-venv" || apt-get install -y python3-venv; }
  rm -rf "$DEST/venv"
  python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

if [ ! -f /etc/default/pricecomparison ]; then
  cat > /etc/default/pricecomparison <<CONF
PC_HOST=0.0.0.0
PC_PORT=$PORT
# HTTPS directly from the panel (or put it behind nginx/caddy):
#PC_SSL_CERT=/etc/letsencrypt/live/panel.example.com/fullchain.pem
#PC_SSL_KEY=/etc/letsencrypt/live/panel.example.com/privkey.pem
CONF
fi

if [ ! -f "$DATA/auth.json" ] || ! grep -q '"hash"' "$DATA/auth.json"; then
  PASS="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  (cd "$DEST" && "$DEST/venv/bin/python" -m pricecomparison --data "$DATA" passwd --username admin --password "$PASS")
  CREATED=1
fi

cp "$SRC/deploy/pricecomparison.service" /etc/systemd/system/pricecomparison.service
systemctl daemon-reload
systemctl enable --now pricecomparison
systemctl restart pricecomparison

sleep 3
if ! systemctl is-active --quiet pricecomparison; then
  echo "The panel did not start. Last log lines:"
  journalctl -u pricecomparison -n 20 --no-pager
  exit 1
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "PriceComparison is running:  http://${IP:-SERVER_IP}:$PORT"
if [ "${CREATED:-0}" = 1 ]; then
  echo "username: admin"
  echo "password: $PASS"
fi
