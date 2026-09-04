# Addventures Finance — invoice checking

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

**[docs/DEPLOY-RENDER.md](docs/DEPLOY-RENDER.md)** — deploying to
`finance.addventuresinc.com`. About 15 minutes, all in a browser, no server
administration. **This is the intended route.**

**[QUICKSTART.md](QUICKSTART.md)** — running it on your own computer instead,
about 10 minutes, Mac and Windows.

**[deploy/DEPLOY.md](deploy/DEPLOY.md)** — a traditional Linux server install,
if it ever needs to live somewhere self-managed.

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
| `app/mail_imap.py` | Reads an ordinary mailbox over IMAP — the simple path, no registration needed. |
| `app/mailbox.py` | The Microsoft Graph alternative, for Exchange / Microsoft 365. |
| `scripts/test_mail.py` | Checks the mailbox connection without filing anything. |
| `app/templates/markup.html` | The marked-up invoice — used for both the screen view and the PDF, so they cannot drift. |
| `app/auth.py` | Shared-password login. |
| `scripts/seed_demo.py` | Realistic demo job, no API key needed. |
| `tests/test_matching.py` | Comparison-engine tests. |
| `tests/test_approval.py` | Three-way match and approval routing tests. |

## Reading invoices from a mailbox

Two backends; fill in whichever suits and set `MAIL_ENABLED=true`.

**IMAP — the simple one.** An ordinary mailbox on your own mail host, the kind
you create in cPanel or Plesk. No app registration, no admin consent, nobody to
wait on.

```
MAIL_ENABLED=true
IMAP_HOST=mail.addventuresinc.com
IMAP_USER=ap@addventuresinc.com
IMAP_PASSWORD=...
```

**Microsoft Graph** — only when the mailbox lives in Exchange / Microsoft 365.
Better isolation, but needs an Entra ID app registration with admin consent. See
the header of `app/mailbox.py` for exactly what to ask for.

Check it before switching the poller on — this files nothing and marks nothing read:

```bash
.venv/bin/python scripts/test_mail.py
```

Then run `scripts/poll_mail.py` on a timer (systemd timer or cron, every 5
minutes). It is a one-shot script rather than a loop, so a failure can never
leave polling silently stopped — the next tick just runs again.

A message is only marked read and moved to `Processed` once **every** attachment
on it has been filed. A transient failure leaves the mail unread to retry, rather
than an invoice disappearing quietly.

## How documents get filed against a job

**Job numbers are six digits, and the first two are the year the job was
opened** — `260000` is the first job of 2026, `250148` the hundred and
forty-ninth of 2025. That shape makes a job number self-identifying, so it is
recognised wherever it is written, with or without a label. Jobs from earlier
years stay active and are recognised too.

In order of precedence:

1. The job number typed into the upload form
2. A job number in the email subject line, body, or the upload note —
   `Job 260000`, `job #260000`, or just `260000` in a sentence
3. A six-digit job number printed on the document itself

**A site address is never treated as a job reference.** Quotes frequently
carry our own office address rather than the site, so filing by address would
collect unrelated jobs from unrelated vendors under whichever job used that
address first — and price every one of them against the wrong quote. The
ship-to address is recorded and shown for context; it decides nothing.

Vendor reference numbers are not mistaken for jobs: ABC Supply's quote number
`2014030903`, their account `2174772`, and New Castle's `07RM0002847012` are
all rejected by the shape. Two different job numbers in one message is treated
as a question rather than an answer, and nothing is filed.

If none is found, the document waits in **Inbox** — and, if the mailbox is
switched on, the system emails the sender to ask (see below). Nothing is
guessed.

**Replacing a master quote:** send `master updated to job 4417` in the subject
line, or tick the box on the upload form. The previous master is kept in the
job's history, and every invoice already on that job is automatically re-checked
against the new pricing.

## When a vendor forgets the job number

Most of them do. Both real vendor quotes on file have the job field blank — on
the ABC Supply one, `PO`, `Ref` and `Job` are all empty.

With `ASK_FOR_JOB_NUMBER=true` and a mailbox configured, a document that
arrives with no job number gets a reply to whoever sent it, asking for one. The
answer files the document automatically, reusing the stored extraction rather
than paying to read it again.

```
quote arrives, no job number   →  reply: "which job is this?"
        ↓
vendor replies "260000"        →  filed against job 260000, priced, done
```

Replying is the only thing this system does that leaves the building, so it is
fenced:

- **Off by default.** No deploy starts emailing suppliers by surprise.
- **Never for uploads** — whoever uploaded it is at the screen.
- **Once per document**, recorded on the record. The poller runs every five
  minutes; without that, one missing job number becomes a dozen emails.
- Marked `Auto-Submitted`, so a vacation responder is not read as an answer.

The reply is matched to the document by the `Message-ID` we asked from, carried
back in `In-Reply-To`. Subject lines get edited, forwarded and reused; a message
ID does not.

**A bare `260000` counts as an answer**, because we asked a direct question. The
question itself contains an example job number, so the reply is cut at a
sentinel line before parsing — otherwise a quoted reply answers the question
with our own example.

## The 13-day cash flow report

One button: **Cash flow → Generate 13-day cash flow report**. What has to go
out, what is expected in, and what the bank balance does over the next thirteen
days, day by day, with the low point called out.

**It works before QuickBooks is connected.** QuickBooks Desktop has no cloud
API — reaching it live needs the Web Connector on an always-logged-in Windows
machine beside the company file, which is weeks of work and somebody else's
permission. But it exports the two reports this needs in about four clicks:

```
Reports › Vendors & Payables  › A/P Aging Detail  → Excel › CSV
Reports › Customers & Receiv. › A/R Aging Detail  → Excel › CSV
```

Attach both, type the bank balance, press the button. A live connection later
becomes a third source (`QuickBooksSource` in `app/accounting.py`) and changes
nothing else — the report cannot tell where the numbers came from.

**Three judgements are built in**, because leaving them out produces a report
that is technically correct and practically misleading:

| | |
|---|---|
| **Overdue bills are counted on day one** | They still have to be paid. Dropping them for falling outside the window is how a forecast says you are fine when you are not. |
| **Overdue receivables are *not* counted as arriving** | The opposite treatment, deliberately. Money we owe is certain; money owed to us that is already late is a collections problem, and assuming it turns up today flatters the forecast. |
| **Invoices held by the three-way match are listed, not scheduled** | Held means disputed. Counting it as leaving overstates the outflow, and paying it would be wrong anyway. This is a view QuickBooks cannot produce, because it does not know an invoice is disputed. |

The report also surfaces early-payment discounts expiring inside the window
(`2/10 net 30` is free money and it gets missed), and anything with no date at
all — listed rather than silently excluded.

Reports are stored as their **inputs** and the forecast is rebuilt on view, so a
correction to the arithmetic fixes every report ever produced, and two people
opening the same report always see the same numbers.

**The opening bank balance is typed in by hand.** Everything else is read.

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
- **No live QuickBooks connection.** The cash flow report runs from A/P and A/R
  aging exports instead (above). A live connector needs a Windows machine beside
  the company file; the source interface for it already exists.
- **No customer invoices in this system**, so receivables come only from the A/R
  export. `LocalSource.receivables()` returns nothing rather than inventing
  something, which would make the forecast look solvent.
- **Extraction accuracy is not yet measured** against a corpus of real documents.
  That is the highest-value next step: collect 30–50 real quote/invoice pairs so
  accuracy can be measured rather than assumed.
