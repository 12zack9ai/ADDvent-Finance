# Getting it running

Two ways. **Running it on your own computer takes about 10 minutes and needs
nobody's permission** — do that first, load your real test documents, and deploy
to the server once you know it reads your vendors' paperwork correctly.

For the server install, see [deploy/DEPLOY.md](deploy/DEPLOY.md).

---

## What you need first

**An Anthropic API key.** Nothing can read a document without it.

1. Go to https://console.anthropic.com
2. Sign in → **API keys** → **Create key**
3. Copy it (starts `sk-ant-`). It is shown once.
4. Add about $20 of credit under **Billing** — at roughly $0.08 per document
   that is a few hundred invoices, which is far more than you need to evaluate it.

**Python 3.11 or newer.**

- **Mac:** already installed. Check with `python3 --version`.
- **Windows:** https://python.org/downloads — **tick "Add Python to PATH"** on the
  first screen of the installer. It is easy to miss and everything fails without it.

---

## Run it on your computer

### Mac

Open Terminal and paste these one at a time:

```bash
git clone https://github.com/12zack9ai/ADDvent-Finance.git
cd ADDvent-Finance
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Open `.env` in a text editor (`open -e .env`) and put your key on the
`ANTHROPIC_API_KEY=` line, so it reads:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Then:

```bash
.venv/bin/python scripts/seed_samples.py          # optional sample data
.venv/bin/uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**.

### Windows

Open PowerShell:

```powershell
git clone https://github.com/12zack9ai/ADDvent-Finance.git
cd ADDvent-Finance
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Put your key on the `ANTHROPIC_API_KEY=` line, save, close Notepad. Then:

```powershell
.venv\Scripts\python scripts\seed_samples.py
.venv\Scripts\uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**.

To stop it, press `Ctrl+C`. To start it again later, `cd` back into the folder
and re-run the last line only.

---

## Loading your test documents

1. **Quote first.** Click **Upload**, choose the vendor's quote, type the job
   number, and tick **"Make this the master quote for the job"**. That becomes
   the price everything on the job is checked against.
2. **Then the invoices** for that job. Upload as many as you like at once.
3. Open the job and click **Review** on any invoice to see the marked-up copy,
   or **Download PDF**.

Reading a document takes a few seconds — the page waits rather than showing a
spinner. A multi-page quote should be uploaded as **one file with all its pages**;
the system reports "page 1 of 2" and warns you when pages are missing.

**To approve anything**, confirm receipt on the job page first — that is the
third leg of the match and it is deliberately not skippable.

---

## Something looks wrong?

| What you see | What it means |
|---|---|
| `invalid x-api-key` | The key in `.env` is wrong, or has a stray space. |
| `credit balance is too low` | Add credit at console.anthropic.com → Billing. |
| Document lands in **Inbox** instead of a job | No job number was found. Assign one there and the vendor's own reference is remembered for next time. |
| Prices read wrong | Send me the document. Every extraction is stored, so it can be examined exactly as the model returned it. |
| `python: command not found` (Windows) | Python was installed without "Add to PATH". Re-run the installer and tick it. |
| Nothing on http://127.0.0.1:8000 | The `uvicorn` line isn't running, or it's on a different port — check the terminal output. |

**On your own computer there is no login** (`APP_PASSWORD` is blank), which is
correct locally. It is **not** correct on a server — `deploy/DEPLOY.md` covers
setting it, and the app logs a warning at startup if it is missing.
