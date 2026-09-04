# Deploying to Render

About 15 minutes, all in a browser. You do this part — it needs your Render
account, which I can't sign into.

Have ready:

- Your **Anthropic API key** and, if it's an organisation-level key, the
  **workspace ID** (`wrkspc_…`)
- A **shared password** for staff to sign in with — make it long
- Access to **DNS for addventuresinc.com** (only for step 5)

---

## 1. Create the service

1. Go to **https://render.com** and sign in **with GitHub** — that way it can
   see the repository without extra setup.
2. **New +** → **Blueprint**.
3. Pick **`12zack9ai/ADDvent-Finance`**. If it isn't listed, click *Configure
   account* and grant Render access to that repo.
4. Render reads `render.yaml` and shows one service, **addvent-finance**.
   Click **Apply**.

### If the Blueprint option isn't offered

Blueprints need Render to see the repository, which it only does if you signed
in **with GitHub**. If you signed up with an email address instead, `render.yaml`
is never read and you create the service by hand — **New +** -> **Web Service** ->
connect the repo. Nothing fills itself in, so set all of this yourself:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Branch | `main` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers` |
| Instance Type | **Starter** (not Free) |
| Health Check Path | `/healthz` |
| Disk | name `finance-data`, 10 GB, mount path `/var/lib/finance-automation` |

Plus the environment variables from `render.yaml`: `PYTHON_VERSION`, `DATA_DIR`,
`BASE_URL`, `SITE_NAME`, and the secrets in step 2.

**`$PORT` is capitalised.** Lowercase `$port` expands to an empty string, so
`--port` eats the next argument and the service dies on boot with
`Invalid value for '--port': '--proxy-headers' is not a valid integer`. The build
passes cleanly first, which makes it look like a code problem. It isn't.

**`DATA_DIR` must equal the disk's mount path.** If they differ, the app writes
to the container's own filesystem instead of the disk, everything works
perfectly, and the next deploy silently erases every document and the database.

## 2. Fill in the secrets

Render prompts for the values deliberately kept out of the repo:

| Setting | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key, starting `sk-ant-` |
| `ANTHROPIC_WORKSPACE_ID` | `wrkspc_…` — **only** if the key is organisation-level; leave blank otherwise |
| `APP_PASSWORD` | the shared password staff will use |
| `IMAP_PASSWORD` | leave blank for now — email is switched on later |

`SECRET_KEY` generates itself. Don't set it by hand.

## 3. Check two things before it builds

These are the two settings that cause real problems if they're wrong:

- **Instance type must be `Starter`, not `Free`.** The free plan sleeps when
  idle, which stops the mailbox being read, and it can't have a disk at all.
- **The disk must be there** — `finance-data`, 10 GB, mounted at
  `/var/lib/finance-automation`. It should already be set from the blueprint.
  **Without it, every deploy wipes the database and every uploaded document.**

Then let it build. First build takes 3–5 minutes.

## 4. Confirm it's alive

When the log says **Live**, open the URL Render gives you
(`addvent-finance-xxxx.onrender.com`) and add `/healthz`:

```json
{"ok": true, "pdf": false, "server_pdf": false, "mail": {"enabled": false}}
```

**`"pdf": false` is expected and fine.** Render's Python image has no Chromium,
so the marked-up invoice is saved using the browser's own Print → Save as PDF.
It uses the same stylesheet, so the file is identical — it just takes one click
from the reader instead of being generated on the server.

Then open the site itself. It should ask for the password.

## 5. Point the subdomain at it

1. In Render: **Settings** → **Custom Domains** → **Add** →
   `finance.addventuresinc.com`
2. Render shows a **CNAME** target like `addvent-finance-xxxx.onrender.com`
3. In your DNS, add:

   | Type | Name | Value |
   |---|---|---|
   | CNAME | `finance` | *(the target Render showed)* |

4. Wait a few minutes. Render issues the certificate automatically.

## 6. Load the real documents

Sign in, then **Upload**:

1. **The master quote first** — pick the file, type the job number, and tick
   **"Make this the master quote for the job"**.
2. **Then the invoices** for that job.
3. Open the job, click **Review** on an invoice to see the marked-up copy.

To approve anything, confirm receipt on the job page first — that's the third
leg of the match and it's deliberately not skippable.

## 7. Switch the mailbox on (when you're ready)

In Render → **Environment**, set `MAIL_ENABLED` to `true` and paste the mailbox
password into `IMAP_PASSWORD`. Save; Render restarts the service.

Then check `/healthz` — the `mail` section fills in:

```json
"mail": {"enabled": true, "alive": true, "seconds_since_success": 42,
         "stale": false, "last_summary": "3 message(s): 2 filed, 1 skipped"}
```

**`"stale": true` is the thing to watch for.** It means a mailbox is configured
but nothing has been read for several cycles — the site keeps serving pages
perfectly while quietly receiving nothing. That is how a background poller fails.

---

## If something goes wrong

| What you see | What it is |
|---|---|
| Build succeeds, then `Invalid value for '--port'` | The start command says `$port`, not `$PORT`. Environment variables are case-sensitive: lowercase `$port` expands to nothing, so `--port` swallows the next argument. Settings -> Build & Deploy -> Start Command. |
| Build fails on `pip install` | Almost always the Python version. `PYTHON_VERSION` should be `3.12.7`. |
| Site loads with no password prompt | `APP_PASSWORD` is blank. Set it and redeploy — urgent if the URL is public. |
| Everyone signed out after each deploy | `SECRET_KEY` isn't set. Let Render generate it. |
| Uploads fail with an API error | The key, or a missing `ANTHROPIC_WORKSPACE_ID` for an org-level key. The error message says which. |
| Data disappeared after a deploy | The disk is missing. **Check this before loading anything real.** |
| Documents land in Inbox, not a job | No job number found. Assign one there; the vendor's own reference is remembered for next time. |
| `"stale": true` in healthz | Mail isn't being read. Run `scripts/test_mail.py` locally with the same settings — it says exactly what failed. |

## Costs

| | Monthly |
|---|---|
| Starter web service | ~$7 |
| 10 GB disk | ~$2.50 |
| Claude, at roughly 500 documents | ~$40 |
| **Total** | **~$50** |

Claude is charged per document read, so it scales with volume rather than being
a fixed fee.
