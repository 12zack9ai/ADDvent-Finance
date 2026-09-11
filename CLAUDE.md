# Working notes for Claude

Read this first, every session. It is the memory that survives between them.
If a fact below turns out to be wrong, **fix it here in the same turn** — a stale
line in this file becomes a wrong answer three sessions from now.

⚠️ **This repository is PUBLIC on GitHub.** No passwords, API keys, tokens, real
vendor pricing, account numbers, or customer addresses in any committed file.
Secrets live in Render's environment and in Zack's password manager only.

---

## This is not part of the Empire

**ADDvent-Finance stands alone.** It is not part of Zack's Empire operation —
ATLAS, ATHENA, Abodivo, the content and video Routines, or anything living
under `C:\Users\User\Documents\Empire` on the device `add-win-12`.

Zack's instruction, plainly: *"This should not be a part of the empire
operation so make sure all of this is excluded from it. No watchdog from the
empire."*

So:

- **No Empire agent watches this app**, and none ever should. No scheduled
  Routine, no remote-device agent, no ATLAS/ATHENA process may monitor, read,
  write to, deploy, or "fix" it.
- **No shared state.** No files under the Empire workspace, no shared device
  folders, no shared queues or lock files.
- **The watchdog inside this app is this app's own.** It runs in the web
  process on Render, watches only this app's mailbox, backup and disk, and
  emails only `ALERT_EMAIL`. It reaches nothing outside and nothing outside
  reaches it. That is the only watchdog this project has, and the only one it
  gets.
- **Do not create a Routine for this app.** If something here needs to happen
  on a schedule, it belongs in `app/scheduler.py` or `app/watchdog.py`, inside
  the app, on the app's own server.

The one legitimate overlap is subject matter, not systems: the quarterly
material-list repricing Routine is also Add Ventures work. It reads a price
list and writes a spreadsheet. It does not touch this app, and must not start.

## Who this is for

**Add Ventures Construction Services** — roofing and construction contractor
working for condo associations. That is the name on every page, email and
letter the app produces (Zack, 2026-09-11: *"We are add ventures construction
services"*); it comes from `SITE_NAME`, and the part before "·" is the company
name. The domain stays addventuresinc.com. Owner: **Zack**, `zmabry@addventuresinc.com`.

The app is **ADDvent-Finance**. It reads vendor and subcontractor documents,
checks every invoice against the quote for its job, and runs the money side of
the business around **job numbers**.

## Where things are

| | |
|---|---|
| Project on disk | `/home/user/finance-automation` — **not** the session cwd |
| Git remote | `https://github.com/12zack9ai/ADDvent-Finance` (public, branch `main`) |
| Live site | **https://finance.addventuresinc.com** |
| Also reachable at | https://addvent-finance.onrender.com |
| Render service | `srv-dadeu67qj5pc738r1mk0` |
| Render workspace | `tea-dadenqv40ujc73e18k1g` (`update_environment_variables` needs this) |
| Data disk | `/var/lib/finance-automation`, 10 GB, `DATA_DIR` |

## The mailbox

**`aifinance@addventuresinc.com`** — this is the address. It is live and polling
every 5 minutes.

| Setting | Value |
|---|---|
| Host (IMAP and SMTP) | `mail.protectedharborinc.com` |
| IMAP | port 993, SSL |
| SMTP | port 587 |
| Login | the mailbox worked with the bare username `aifinance` |
| Password | **not written down here — public repo.** It is in `IMAP_PASSWORD` / `SMTP_PASSWORD` on Render, and in Zack's password manager |
| Replies | `REPLY_DOMAINS=addventuresinc.com` — the app may email **only** @addventuresinc.com addresses, never anyone outside |

Mail hosting is run by **Protected Harbor**, the IT company. Contact there:
**Edwin**. They also hold the QuickBooks company file.

> The `.env` in this working copy is a template with placeholder values. **Render
> is the source of truth for live configuration**, not `.env`. Do not read
> `.env` and report it as the running config — that mistake has already been
> made once and produced two wrong answers to Zack.

## DNS — read this before touching a domain record

**The authoritative nameservers for `addventuresinc.com` are Register.com**
(`dns101.register.com`, `dns102.register.com`). Every DNS record has to be made
there.

A2 Hosting serves the website at `68.66.224.16` and its cPanel has a Zone
Editor for the domain — **that zone file is dead.** Nothing on the internet
reads it. A record added there looks saved, shows up in the list, and never
resolves. An hour went into this once; do not spend it again.

`finance.addventuresinc.com` points at the app and is live as of 2026-09-08:

    CNAME   finance   ->   addvent-finance.onrender.com

added at Register.com, then verified in the Render dashboard under the
**service's** Settings (`/web/srv-.../settings`) — not the workspace settings,
which have no custom-domain section at all.

A dead CNAME for `finance` also sits in A2's cPanel zone. It does nothing.
Delete it if it ever confuses anybody.

Never move the nameservers to A2 to make this easier. The MX records point at
Protected Harbor, and moving the nameservers takes the mailbox down with them.

## Rules Zack has set

- **Everything has a job number.** *"Job numbers are how we do everything."*
  Six digits, first two are the year. Old numbers stay valid — `250148` is the
  149th job of 2025 and still works.
- **The app never emails outside the company.** *"It should never send out
  another email to an outside source. At least not yet."*
- **Real vendor documents are never committed** — they carry real pricing,
  account numbers and site addresses.
- **Passwords are not being rotated.** Zack decided this: *"Nobody else is using
  this except employees in our company so I'm not worried about it."* Do not
  raise it again.
- **Keep it simple on screen.** *"I can't have it be confusing for the dinosaur
  I'm working with."* Four numbers, one list, and drawers for everything else.
  No walls of text.

## JobNimbus — who gets asked

The app asks JobNimbus **one question**: an invoice arrived on a job with no
quote, so who should be asked for it?

**Only the "Assigned to" person.** Zack: *"Do not harass the sales rep. As
they are not responsible for collecting invoices or what not. The assigned to
is typically the project manager, who is responsible for that. Only."* The
sales rep is not a fallback and must never become one — `NEVER_EMAIL_KEYS` in
`app/jobnimbus.py` exists to name that field so nothing quietly starts reading
it. A job with nobody assigned reaches nobody, and that is the correct
outcome: it is a gap to fill in JobNimbus, not a licence to write to whoever
else is on the record.

Nothing else is taken from JobNimbus. No customer details, no addresses, no
money, no schedule. One lookup, one name, one email address.

**The six digits are the identity.** Job titles read like
`241640- Mountainview Condos (due 10-30-24)`. Every job carries the six-digit
number and that is what the app keys on; the words after it are a label. A
date like `10-30-24` is not mistaken for one, because the year-prefix rule in
`app/jobnum.py` rejects it.

## The design rule that holds the whole thing up

**Claude reads. Python does the arithmetic.** The model extracts values from a
PDF; every comparison, sum, and variance is computed in `decimal.Decimal`. A
model must never be the thing that decides whether `$4,182.60 != $4,128.60`.

## Items not on the quote

Zack, 2026-09-10: *"invoice one comes in but didnt have a 3" pipe boot on it
but it was $65. invoice two comes in with the pipe boot at $66 dollars.....
thats a problem... need an easy way to see that"*

Nothing prices an item the quote left out, so **the first invoice that bills it
on a job sets its price**, and every later invoice from the same supplier on
that job is held to it (`app/pricewatch.py`). Either direction is flagged: a
red "price changed · first billed $65" on the line, a "price changed" chip on
the job page and the Invoices folder, and an owner spot check. Oldest first by
the printed invoice date; a rejected invoice, or one with no supplier name,
sets nothing. Once a quote prices the item, the quote decides and the flag goes.

**The invoice says what each line was compared to** when the wording differs
from the quote line it matched ("on the quote as: … Weathered Wood" under
"… Charcoal"). A shared part number or a close description is how a colour
swap gets priced, and the price matching says nothing about the colour.
`matching.worded_differently` decides; case and punctuation alone don't count.

**Colours pair on their own** (`matching._COLOR_PHRASES`, job 261216): with
the colour names out of both descriptions, the same words are the same item,
so Charcoal is priced against a Weathered Wood quote line and the invoice says
"· different color". Exact words, never a similarity score, and a word that is
also a material (copper, slate, clay, sand) only counts inside a colour name.
A colour that slips through goes on the list.

**"Same item" is a person's pairing** (`ItemMatch`): on a grey line, pick the
quote line it is. Remembered per job and supplier by part number (else
wording), so later invoices billing it pair without a click; "not the same
item" undoes it. It lapses if its quote line is replaced.

**Re-comparing never undoes a person's decision.** `apply_routing` leaves
approved, paid and rejected invoices alone, and a hold a person placed (their
last Approval is "hold"). It didn't: every new quote on a job put that job's
rejected invoices back to Pending and released anything held by hand.

**A matcher change reaches what is already filed** through `_recheck_once` in
`app/main.py`: on the first boot after `RECHECK_VERSION` changes, every job is
re-compared once, off the startup path, and a marker on the data disk stops it
running again. Bump the version when a change should re-price old invoices.
"Not on the quote" at the top of the job page is `jobsummary.off_quote` -
worked out from line verdicts on every view - so pairing a line lowers it.

**Quote-only jobs stay off the Invoices page.** Zack, 2026-09-10: *"i like it
in the jobs. the quote should be pulled in when we get invoices to not drown
that area."* A job with just a quote is on **Jobs**; it joins **Invoices** with
its first invoice. `/incoming` joining on `Invoice` is the design, not a bug.

## The six departments

1. **Vendor invoicing** — invoice against the job's master quote.
2. **Subcontractor invoicing** — sub quote attached to a job number; a sub
   invoice **automatically raises a check request**. One invoice is both.
3. **Check requests** — the queue is a *union* of sub invoices and standalone
   requests, derived and never written, so nothing is counted twice.
4. **Job costing** — billed and collected come from **QuickBooks**. The only
   manual entry is our own crew: hours and cost, and only when the job is not
   fully subbed out.
5. **Cash flow** — 13-week forecast, including unpaid check requests, permits
   and deposits.
6. **Receipt collector** — photograph a receipt, email or text it in, the system
   asks which job, it lands in that job's folder. Money spent on jobs we did not
   win is tracked separately as a **loss**, not a job cost.

## Environment variables (names only — values live on Render)

`ANTHROPIC_API_KEY`, `port`, `DATA_DIR`,
`MAIL_ENABLED`, `MAIL_BACKEND`, `IMAP_HOST/PORT/SSL/USER/PASSWORD/FOLDER`,
`SMTP_HOST/PORT/USER/PASSWORD/FROM`, `REPLY_DOMAINS`,
`ASK_FOR_JOB_NUMBER`, `ASK_FOR_QUOTE`, `REQUIRE_RECEIPT`,
`LOAD_SAMPLES`, `SEED_SAMPLES`, `RESET_SAMPLES`,
`QUICKBOOKS_ENABLED`, `QBWC_PASSWORD`,
`BACKUP_ENABLED`, `BACKUP_HOUR`, `BACKUP_KEEP`, `BACKUP_DOCUMENTS`,
`SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_BUCKET`,
`ALERT_EMAIL`, `DISK_WARN_BELOW`.

`BASE_URL` is `https://finance.addventuresinc.com`. It decides whether the login
cookie is marked Secure, and it is embedded in the QuickBooks `.qwc` file we
hand to the IT company — so the `.qwc` must be generated from this value, not
from the onrender.com address.

**Sample data flags are all off and the app is empty of samples** as of
2026-09-08 — Zack is loading real documents. `ALERT_EMAIL` is
`zmabry@addventuresinc.com`.

## QuickBooks

**Desktop, and there is no cloud API.** Anything sold as one is a wrapper. The
only official remote path is the **Web Connector (QBWC)**, which inverts
control: QuickBooks polls us over SOAP; we never call QuickBooks.

Built and tested, waiting on a Windows machine: `app/quickbooks/` — the eight
QBWC callbacks, qbXML build and parse, the mirror tables, the outbox, and a
`.qwc` generator. See `docs/QUICKBOOKS-SETUP.md`, which is written for
Protected Harbor.

Three questions still outstanding with them: where the company file physically
lives, whether they will provision an always-logged-in Windows VM and at what
cost, and their policy on third-party SDK access.

## Safety net

- **Nightly backup** at 07:00 UTC: one zip with the database and every stored
  document, 14 kept on the disk, downloadable at `/backup`. Uploaded to
  Supabase Storage when `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_BUCKET` are
  set — **they are not yet**, so today the only off-server copy is one somebody
  downloads.
- **Watchdog** every 15 minutes: mailbox stale, backup failed or out of date,
  disk nearly full. Emails `ALERT_EMAIL` once per incident and once on
  recovery, and shows a banner on every page. Alerts obey `REPLY_DOMAINS` like
  everything else — an address outside the company is refused.
- **Login** allows five wrong tries per address, then a doubling delay capped
  at a minute. A delay, never a lockout.
- `/healthz` reports mail, backup, disk and any open alarm. It is the thing to
  point an uptime monitor at.

## Restoring

A backup is a plain zip: `finance.db` plus `documents/`. Stop the app and run:

    python scripts/restore_backup.py latest
    python scripts/restore_backup.py <file.zip> --into /var/lib/finance-automation

It refuses a zip that is not this app's database before it touches anything,
moves the WAL sidecars aside (left in place, SQLite replays them over the
restore and silently undoes it), and keeps whatever it replaced as
`*.superseded-<stamp>` rather than deleting it.

**Rehearsed end to end on 2026-09-08**: a populated data directory was deleted
outright, restored from the zip, and the app started against it — job page,
invoice figures and the original PDF all came back. It is a procedure now, not
a hope. Worth repeating against production data before anyone relies on it.

## Money asked back

An invoice billed above its quote offers **Ask for it back**: a composed letter
with the lines, quoted against billed, and the total. It is a draft for a person
to send from their own mail — the app never emails a vendor. `/disputes` records
what was asked and what came back, grouped by vendor.

## Still open

- **JobNimbus API key** — site-wide, expected from Zack. Set
  `JOBNIMBUS_API_KEY` on Render, then open **`/jobnimbus`** and look up a real
  job number. It prints every key on the record and says which of the
  candidate field lists in `app/jobnimbus.py` matched nothing — trim them to
  what is really there. `scripts/jobnimbus_probe.py` does the same from a
  terminal, but this sandbox cannot reach `app.jobnimbus.com` at all, so the
  page is the reliable route.
- Do subs bill **AIA-style (G702/G703)** or a flat percentage of one contract
  number? This gates sub email ingestion.
- Customer-side change orders — specified in `docs/subcontractor-check-requests.md`,
  not built.
- Retainage on subs and lien waivers — raised, never confirmed.
- Custom domain `finance.addventuresinc.com` — not set up.
- **Supabase is not wired up.** Until it is, the only copy off this server is
  one somebody downloads from `/backup`.
- Per-user accounts. One shared password means `Approval.actor` and
  `Dispute.raised_by` are whatever gets typed.

## Before pushing

```
cd /home/user/finance-automation && .venv/bin/python -m pytest -q    # 744 passing as of 2026-09-10
```
