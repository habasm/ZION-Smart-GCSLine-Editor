#!/usr/bin/env bash
set -euo pipefail

VPS_IP="your-ip"
APP_USER="lookingforitknow"
APP_DIR="/home/lookingforitknow/zion-editor"
APP_FILE="${APP_DIR}/editor.py"
WEBROOT="/var/www/html"
CERTBOT_VENV="/opt/zion-certbot"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy_ip_https.sh YOUR_EMAIL"
  exit 1
fi

EMAIL="${1:-}"
if [[ -z "${EMAIL}" ]]; then
  echo "Provide an email for certificate notices."
  echo "Example: sudo bash deploy_ip_https.sh you@example.com"
  exit 1
fi

if [[ ! -f "${APP_FILE}" ]]; then
  echo "Application not found: ${APP_FILE}"
  exit 1
fi

apt-get update
apt-get install -y nginx python3 python3-venv

python3 -m venv "${CERTBOT_VENV}"
"${CERTBOT_VENV}/bin/pip" install --upgrade pip "certbot>=5.4"
ln -sf "${CERTBOT_VENV}/bin/certbot" /usr/local/bin/certbot

APP_PYTHON="$(sudo -u "${APP_USER}" bash -lc 'command -v python3')"
if [[ -z "${APP_PYTHON}" ]]; then
  echo "Could not locate python3 for ${APP_USER}."
  exit 1
fi

cat > /etc/systemd/system/zion-editor.service <<EOF
[Unit]
Description=ZION Gradio Dataset Editor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_PYTHON} ${APP_FILE}
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/zion-editor <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${VPS_IP};

    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type text/plain;
    }

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Host \$http_host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        client_max_body_size 2g;
    }
}
EOF

ln -sf /etc/nginx/sites-available/zion-editor /etc/nginx/sites-enabled/zion-editor
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl daemon-reload
systemctl enable --now zion-editor

certbot certonly \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path "${WEBROOT}" \
  --ip-address "${VPS_IP}" \
  --non-interactive \
  --agree-tos \
  --email "${EMAIL}"

cat > /etc/nginx/sites-available/zion-editor <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${VPS_IP};

    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type text/plain;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${VPS_IP};

    ssl_certificate /etc/letsencrypt/live/${VPS_IP}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${VPS_IP}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_timeout 1d;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Host \$http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        client_max_body_size 2g;
    }
}
EOF

cat > /etc/systemd/system/zion-certbot-renew.service <<EOF
[Unit]
Description=Renew ZION short-lived IP certificate

[Service]
Type=oneshot
ExecStart=/usr/local/bin/certbot renew --quiet
ExecStartPost=/usr/bin/systemctl reload nginx
EOF

cat > /etc/systemd/system/zion-certbot-renew.timer <<EOF
[Unit]
Description=Check ZION IP certificate renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:15:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

nginx -t
systemctl reload nginx
systemctl daemon-reload
systemctl enable --now zion-certbot-renew.timer

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full'
fi

echo
echo "ZION editor is configured at https://${VPS_IP}"
echo "Check app:  systemctl status zion-editor"
echo "Check HTTPS: curl -I https://${VPS_IP}"
