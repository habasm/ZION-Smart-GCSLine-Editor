# ZION editor HTTPS deployment

Target address: `https://34.56.252.248`

## Before running the installer

In the Google Cloud firewall, allow inbound TCP ports **80** and **443** for
this VM. Keep Gradio port **7860** closed publicly; Nginx reaches it through
localhost.

Copy `editor.py` and `deploy_ip_https.sh` to:

`/home/lookingforitknow/zion-editor/`

## Install

Run on the VPS:

```bash
cd /home/lookingforitknow/zion-editor
chmod +x deploy_ip_https.sh
sudo ./deploy_ip_https.sh YOUR_REAL_EMAIL
```

The installer configures:

- Gradio as a persistent `systemd` service on `127.0.0.1:7860`.
- Nginx on ports 80 and 443 with WebSocket support.
- A trusted Let's Encrypt short-lived certificate for `34.56.252.248`.
- Twice-daily automatic certificate renewal checks.
- HTTP-to-HTTPS redirection.

## Verify

```bash
sudo systemctl status zion-editor
sudo systemctl status nginx
sudo systemctl status zion-certbot-renew.timer
curl -I https://34.56.252.248
```

Then open `https://34.56.252.248` in a browser.

## Troubleshooting

```bash
sudo journalctl -u zion-editor -n 100 --no-pager
sudo journalctl -u nginx -n 100 --no-pager
sudo nginx -t
```

If certificate issuance fails, confirm that the IP is still attached to this
VM and that port 80 is reachable from the public Internet.
