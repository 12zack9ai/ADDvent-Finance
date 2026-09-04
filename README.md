# Adventures Finance — invoice checking

Checks every vendor invoice against the master quote for its job, and produces a
marked-up copy showing exactly where the pricing differs.

- **Red** — billed above the quoted unit price, with the quoted price shown beneath
- **Green** — billed below the quoted price, with the quoted price shown beneath
- **Gold** — exactly as quoted
- **Grey** — not on the master quote, so nothing was checked

## How it works

```
quote or invoice arrives          →  emailed to the finance inbox, or uploaded
        ↓
read into line items              →  Claude (claude-opus-5), PDF read directly
        ↓
filed against a job number        →  from the subject line, the note, or the document
        ↓
quote?   becomes the MASTER for that job (and re-checks existing invoices)
invoice? every unit price compared against the master
        ↓
marked-up copy                    →  on screen and as a PDF
```

**The master quote is a price list for the job, not a budget.** Materials arrive
in several deliveries, so one quoted item is legitimately billed across many
invoices. Each one is priced against the same master.

## The one rule worth knowing

**Claude reads. Python does the arithmetic.**

The AI only turns a picture of a document into a list of numbers. Every
comparison, subtraction and total is ordinary `Decimal` arithmetic in
`app/matching.py`, so the same inputs always produce the same answer and the
logic is unit-tested. The AI never decides whether two numbers differ.

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY
.venv/bin/python scripts/seed_demo.py --reset    # optional demo data
.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000. With `APP_PASSWORD` unset there is no login,
which is fine on localhost and **not** fine anywhere else.

To deploy to `finance.adventuresinc.com`, see **[deploy/DEPLOY.md](deploy/DEPLOY.md)**.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

The comparison engine is where a bug quietly costs money, so that is what the
tests cover: every verdict, the dollar-impact arithmetic, fractional-cent unit
prices, and the cases where a match must be *refused* rather than guessed.

They need no API key and no network — the engine is pure arithmetic by design.

## Layout

| Path | What it is |
|---|---|
| `app/extract.py` | Reads a PDF into structured line items. The only file that calls Claude. |
| `app/matching.py` | Compares invoice lines to quote lines. **No AI, no network.** |
| `app/services.py` | The pipeline: store, de-duplicate, file against a job, compare. |
| `app/mailbox.py` | Reads the Exchange mailbox over Microsoft Graph. Header explains what to ask IT for. |
| `app/templates/markup.html` | The marked-up invoice — used for both the screen view and the PDF, so they cannot drift. |
| `app/auth.py` | Shared-password login. |
| `scripts/seed_demo.py` | Realistic demo job, no API key needed. |
| `tests/` | Comparison-engine tests. |

## How documents get filed against a job

In order of precedence:

1. The job number typed into the upload form
2. A job number in the email subject line or the upload note —
   `Job 4417`, `job #4417`, `job number 4417`
3. A job number printed on the document itself

If none is found, the document waits in **Inbox** for someone to assign one.
Nothing is guessed.

**Replacing a master quote:** send `master updated to job 4417` in the subject
line, or tick the box on the upload form. The previous master is kept in the
job's history, and every invoice already on that job is automatically re-checked
against the new pricing.

## Current limitations

Worth being straight about:

- **One shared password**, not per-user accounts. Fine for a small team; see
  `app/auth.py` for the upgrade path to Microsoft SSO through the same app
  registration the mailbox uses.
- **No approval workflow yet.** The system flags pricing differences; it does not
  track who accepted or disputed one.
- **SQLite.** Correct and fast at this volume, single-server only. Moving to
  Postgres is a `DATABASE_URL` change.
- **No QuickBooks connection.** Deliberately deferred — see the separate 13-day
  cash-flow plan.
- **Extraction accuracy is not yet measured** against a corpus of real documents.
  That is the highest-value next step: collect 30–50 real quote/invoice pairs so
  accuracy can be measured rather than assumed.
