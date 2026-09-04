# Deploying to finance.adventuresinc.com

Written to be handed to whoever administers the server. Assumes Debian/Ubuntu
with systemd and nginx. Budget about 30 minutes.

## What this needs

- A Linux server (2 vCPU / 4 GB RAM is plenty) reachable on ports 80 and 443
- Python 3.11 or newer
- Chromium (for generating the marked-up PDFs)
- A DNS `A` record: `finance.adventuresinc.com` -> the server's IP
- An Anthropic API key (https://console.anthropic.com)

Outbound HTTPS to `api.anthropic.com` must be allowed. If the mailbox is used,
also `login.microsoftonline.com` and `graph.microsoft.com`. No inbound access is
needed other than the web traffic itself.

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx chromium certbot python3-certbot-nginx
```

If `chromium` is unavailable, `chromium-browser` or `google-chrome-stable` also
work — set `CHROME_BINARY` in `.env` to whichever you installed.

## 2. Application user and files

```bash
sudo useradd --system --home /opt/finance-automation --shell /usr/sbin/nologin finance
sudo mkdir -p /opt/finance-automation /var/lib/finance-automation
sudo rsync -a ./ /opt/finance-automation/        # or: git clone
sudo chown -R finance:finance /opt/finance-automation /var/lib/finance-automation
```

`/var/lib/finance-automation` holds the database, every uploaded document, and
the generated PDFs. **This is the directory to back up.** Nothing else on the
box carries state.

## 3. Virtualenv

```bash
cd /opt/finance-automation
sudo -u finance python3 -m venv .venv
sudo -u finance .venv/bin/pip install --upgrade pip
sudo -u finance .venv/bin/pip install -r requirements.txt
```

## 4. Configuration

```bash
sudo -u finance cp .env.example .env
sudo -u finance nano .env
sudo chmod 600 /opt/finance-automation/.env
```

Set at minimum:

| Setting | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com |
| `APP_PASSWORD` | a long shared passphrase for staff |
| `SECRET_KEY` | `openssl rand -base64 48` |
| `BASE_URL` | `https://finance.adventuresinc.com` |
| `DATA_DIR` | `/var/lib/finance-automation` |

Leave `MAIL_ENABLED=false` until IT provides the Graph credentials. The app is
fully usable via upload until then.

> **`APP_PASSWORD` and `SECRET_KEY` are not optional in production.** With
> `APP_PASSWORD` blank the site is open to anyone who can reach the URL. The app
> logs a warning at startup if either is missing — check the logs after starting.

## 5. Run it as a service

```bash
sudo cp deploy/finance-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-app
sudo systemctl status finance-app
curl -s localhost:8000/healthz     # {"ok":true,"pdf":true,...}
```

`pdf:true` confirms Chromium was found. If it is false, set `CHROME_BINARY`.

## 6. nginx and TLS

```bash
sudo cp deploy/nginx-finance.conf /etc/nginx/sites-available/finance
sudo ln -s /etc/nginx/sites-available/finance /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d finance.adventuresinc.com
```

Certbot rewrites the config for TLS and installs a renewal timer.

## 7. Mailbox polling (once IT provides credentials)

Set the `MS_*` values and `MAIL_ENABLED=true` in `.env`, then:

```bash
sudo cp deploy/finance-mail.service deploy/finance-mail.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-mail.timer
sudo systemctl restart finance-app

sudo systemctl start finance-mail       # run once now
sudo journalctl -u finance-mail -n 50   # check what it did
```

The timer runs every 5 minutes. It is a one-shot script rather than a loop, so a
failure can never leave polling silently stopped — the next tick just runs again.

What to ask IT for is documented at the top of `app/mailbox.py`.

## 8. Backups

```
/var/lib/finance-automation
```

Nightly is enough. **Test a restore** — an untested backup is a guess. Everything
else on the server can be rebuilt from this repository in 20 minutes.

## Day-to-day

```bash
sudo systemctl restart finance-app        # after a config change
sudo journalctl -u finance-app -f         # follow the logs
sudo journalctl -u finance-mail --since today
curl -s localhost:8000/healthz
```

To deploy a code update:

```bash
cd /opt/finance-automation
sudo -u finance git pull
sudo -u finance .venv/bin/pip install -r requirements.txt
sudo systemctl restart finance-app
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `healthz` shows `"pdf": false` | Chromium not found. Install it, or set `CHROME_BINARY`. |
| Documents fail with an API error | `ANTHROPIC_API_KEY` missing, invalid, or outbound HTTPS blocked. |
| Everyone signed out after a restart | `SECRET_KEY` not set, so a random one is generated each boot. |
| Site loads without asking for a password | `APP_PASSWORD` is blank. Set it and restart — this is urgent if the site is public. |
| Mail poll reports an auth error | Admin consent not granted on the app registration, or the secret expired. Client secrets expire — note the date. |
| 502 from nginx | The app service is not running: `systemctl status finance-app`. |
