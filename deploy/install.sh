#!/usr/bin/env bash
# Install or update the bot on a server. Run from the project directory:
#   bash deploy/install.sh
# Safe to run again after `git pull`: it reinstalls requirements, reruns the checks
# and restarts the service. It never touches anything outside this directory except
# /etc/systemd/system/buybot.service.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

# Leave room for everything else on the server.
MIN_FREE_MB="${MIN_FREE_MB:-400}"
free_mb=$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)
if [ "${free_mb:-0}" -lt "$MIN_FREE_MB" ]; then
  echo "Only ${free_mb} MB of memory available (need ${MIN_FREE_MB}); not installing."
  exit 1
fi
UNIT=/etc/systemd/system/buybot.service
if [ -f "$UNIT" ] && ! grep -qF "WorkingDirectory=${DIR}" "$UNIT"; then
  echo "$UNIT belongs to another directory; not overwriting it."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env: fill in BOT_TOKEN and OWNER_IDS, then run this script again."
  exit 1
fi
chmod 600 .env

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is needed (found $(python3 --version))."
  exit 1
fi
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo "python3-venv is missing: apt install -y python3-venv"
  exit 1
fi
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "== offline tests"
venv/bin/python tests.py
venv/bin/python tests_bot.py

echo "== live checks against Robinhood Chain"
if ! venv/bin/python tools/preflight.py; then
  echo "Preflight failed: read the lines above before starting the bot."
  exit 1
fi

mkdir -p data
sed "s#__DIR__#${DIR}#g" deploy/buybot.service > "$UNIT"
# Optional server-specific restrictions (kept out of the public repo).
if [ -f deploy/server-hardening.conf ]; then
  mkdir -p "${UNIT}.d"
  cp deploy/server-hardening.conf "${UNIT}.d/hardening.conf"
fi
systemctl daemon-reload
systemctl enable buybot >/dev/null
systemctl restart buybot
sleep 3
systemctl --no-pager --lines=15 status buybot
