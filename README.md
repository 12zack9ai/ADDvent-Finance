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

## Three-way match

No invoice is paid unless it ties back to **(1)** an approved quoted price and
**(2)** confirmation the material or work was actually received. Price alone is
not enough — an invoice can be perfectly priced against the quote and still bill
for a delivery that never arrived.

```
quote / master price list ──┐
receiving confirmation    ──┼──▶  routed to an approver  ──▶  approved  ──▶  paid
vendor invoice            ──┘
```

**Approval routing** (`app/approval.py`, thresholds in `.env`):

| Situation | Approver | Action |
|---|---|---|
| Within tolerance (5% or $250, whichever is greater) | Project / office manager | Approve |
| Within tolerance but over $5,000 | Owner | Spot check |
| Over tolerance, no change order on file | Owner | **Held** |
| No quote or PO from that vendor on that job | Owner | **Investigate** |

A **missing receipt confirmation blocks approval outright**, whatever the price
says. A **change order** covering the overage releases a held invoice
automatically — record it on the job page and anything it authorises is freed.

Two things that are easy to conflate, and are deliberately separate:

- The **colours** on the marked-up invoice are literal. A one-cent difference is
  shown as a difference, because the reader deserves the truth.
- The **tolerance** decides *who has to look*. A $3 variance on a $40,000 order
  is not worth the owner's attention.

Every decision is written to an append-only `approval` record — who, when, what
the system recommended, and what the variance was at that moment. When a condo
board asks for backup on a large assessment, the quote, receipt, invoice,
marked-up copy and approval trail are one page, not a scramble.

## The one rule worth knowing

**Claude reads. Python does the arithmetic.**

The AI only turns a picture of a document into a list of numbers. Every
comparison, subtraction and total is ordinary `Decimal` arithmetic in
`app/matching.py`, so the same inputs always produce the same answer and the
logic is unit-tested. The AI never decides whether two numbers differ.

## Getting it running

**[QUICKSTART.md](QUICKSTART.md)** — running it on your own computer, about 10
minutes, Mac and Windows, no server needed. Start here.

**[deploy/DEPLOY.md](deploy/DEPLOY.md)** — the server install at
`finance.adventuresinc.com`, for whoever administers it.

The short version:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY
.venv/bin/python scripts/seed_demo.py --reset    # optional demo data
.venv/bin/uvicorn app.main:app --port 8000
```

With `APP_PASSWORD` unset there is no login, which is fine on localhost and
**not** fine anywhere else.

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
| `app/approval.py` | Three-way match and approval routing. **No AI, no network.** |
| `app/services.py` | The pipeline: store, de-duplicate, file against a job, compare. |
| `app/mailbox.py` | Reads the Exchange mailbox over Microsoft Graph. Header explains what to ask IT for. |
| `app/templates/markup.html` | The marked-up invoice — used for both the screen view and the PDF, so they cannot drift. |
| `app/auth.py` | Shared-password login. |
| `scripts/seed_demo.py` | Realistic demo job, no API key needed. |
| `tests/test_matching.py` | Comparison-engine tests. |
| `tests/test_approval.py` | Three-way match and approval routing tests. |

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
- **Approver identity is typed, not authenticated.** One shared password means
  the app cannot know who is signed in, so approvers type their name and it is
  recorded as-typed. Real segregation of duties needs per-user logins — see
  `app/auth.py` for the upgrade path.
- **Receipt matching is by job and vendor, not line by line.** It records that
  somebody confirmed this vendor delivered on this job. Reconciling packing
  slips item by item is a bigger job and nobody was going to do it by hand either.
- **SQLite.** Correct and fast at this volume, single-server only. Moving to
  Postgres is a `DATABASE_URL` change.
- **No QuickBooks connection.** Deliberately deferred — see the separate 13-day
  cash-flow plan.
- **Extraction accuracy is not yet measured** against a corpus of real documents.
  That is the highest-value next step: collect 30–50 real quote/invoice pairs so
  accuracy can be measured rather than assumed.
