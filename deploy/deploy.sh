#!/usr/bin/env bash
# Idempotent deploy/update for the SoD Loot Checker on nx01.
# Run as root (or with sudo) on the server:  sudo bash /opt/sod-loot/deploy/deploy.sh
set -euo pipefail

APP_DIR=/opt/sod-loot
REPO=https://github.com/ajrbyers/sod-loot.git
SVC=sod-loot

echo ">> Clone/update repo"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo ">> Python venv + deps"
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp deploy/env.production.example .env
    echo "!! Created $APP_DIR/.env from the example — EDIT IT with real secrets, then re-run."
    exit 1
fi

echo ">> Cache table + static files"
.venv/bin/python manage.py createcachetable
.venv/bin/python manage.py collectstatic --noinput

echo ">> Permissions"
chown -R www-data:www-data "$APP_DIR"

echo ">> systemd service"
cp deploy/sod-loot.service /etc/systemd/system/${SVC}.service
systemctl daemon-reload
systemctl enable "$SVC"
systemctl restart "$SVC"

echo ">> nginx site"
if [ ! -f /etc/nginx/sites-available/${SVC} ]; then
    cp deploy/nginx-sod-loot.conf /etc/nginx/sites-available/${SVC}
    ln -sf /etc/nginx/sites-available/${SVC} /etc/nginx/sites-enabled/${SVC}
else
    # Left in place so certbot's HTTPS edits survive re-deploys. To reset it,
    # rm the file and re-run.
    echo "   nginx site already exists — leaving it untouched (preserves SSL config)."
fi
nginx -t
systemctl reload nginx

echo ">> Done. Status:"
systemctl --no-pager --lines=5 status "$SVC" || true
