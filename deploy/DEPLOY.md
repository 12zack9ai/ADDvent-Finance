# Deploying to finance.addventuresinc.com

Written to be handed to whoever administers the server. Assumes Debian/Ubuntu
with systemd and nginx. Budget about 30 minutes.

## What this needs

- A Linux server (2 vCPU / 4 GB RAM is plenty) reachable on ports 80 and 443
- Python 3.11 or newer
- Chromium (for generating the marked-up PDFs)
- A DNS `A` record: `finance.addventuresinc.com` -> the server's IP
- An Anthropic API key (https://console.anthropic.com)

Outbound HTTPS to `api.anthropic.com` must be allowed, plus IMAP (port 993) to
our own mail server once the mailbox is switched on. No inbound access is needed
other than the web traffic itself.

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
| `BASE_URL` | `https://finance.addventuresinc.com` |
| `DATA_DIR` | `/var/lib/finance-automation` |

Leave `MAIL_ENABLED=false` until the mailbox is ready (step 7). The app is fully
usable via upload until then.

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
sudo certbot --nginx -d finance.addventuresinc.com
```

Certbot rewrites the config for TLS and installs a renewal timer.

## 7. Mailbox polling

The mailbox is an ordinary account on our own mail hosting, read over IMAP —
there is no app registration or admin consent involved. Set these in `.env`:

```
MAIL_ENABLED=true
IMAP_HOST=mail.protectedharborinc.com   # Protected Harbor's own mail server
IMAP_PORT=993
IMAP_USER=aifinance@addventuresinc.com     # the FULL address
IMAP_PASSWORD=...
```

Check it before switching the timer on — this files nothing and marks nothing read:

```bash
sudo -u finance .venv/bin/python scripts/test_mail.py
```

Then restart the app:

```bash
sudo systemctl restart finance-app
curl -s localhost:8000/healthz | python3 -m json.tool
```

**Polling runs inside the app itself** — no second service, no cron, no timer.
It shares the database and document store, which a separate service on a managed
host generally cannot reach.

The usual risk with a background loop is that it dies quietly and nobody notices
the invoices stopped arriving, so `/healthz` reports it:

```json
"mail": { "enabled": true, "alive": true, "seconds_since_success": 42,
          "stale": false, "last_summary": "3 message(s): 2 filed, 1 skipped" }
```

**Alert on `"stale": true`.** That means a mailbox is configured but nothing has
been read for several cycles — the site still serves pages perfectly while
quietly receiving nothing. Repeated failures back off automatically so a wrong
password doesn't hammer the mail server.

*(`deploy/finance-mail.service` and `.timer` remain for running polls out of
process if you ever prefer that. They are not needed for a normal install.)*

(If the mailbox ever moves to Microsoft 365, `app/mailbox.py` holds the Graph
backend and its header lists what to ask IT for. Nothing to do while it is on
our own hosting.)

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
| Mail poll reports an auth error | Usually `IMAP_USER` is missing the `@domain` part — it must be the full address. Run `scripts/test_mail.py`, which says exactly what failed. |
| 502 from nginx | The app service is not running: `systemctl status finance-app`. |
