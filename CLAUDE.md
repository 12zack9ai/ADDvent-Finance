# Working notes for Claude

Read this first, every session. It is the memory that survives between them.
If a fact below turns out to be wrong, **fix it here in the same turn** — a stale
line in this file becomes a wrong answer three sessions from now.

⚠️ **This repository is PUBLIC on GitHub.** No passwords, API keys, tokens, real
vendor pricing, account numbers, or customer addresses in any committed file.
Secrets live in Render's environment and in Zack's password manager only.

---

## Who this is for

**Add Ventures Inc.** — roofing and construction contractor working for condo
associations. Owner: **Zack**, `zmabry@addventuresinc.com`.

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

## The design rule that holds the whole thing up

**Claude reads. Python does the arithmetic.** The model extracts values from a
PDF; every comparison, sum, and variance is computed in `decimal.Decimal`. A
model must never be the thing that decides whether `$4,182.60 != $4,128.60`.

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
`QUICKBOOKS_ENABLED`, `QBWC_PASSWORD`.

`BASE_URL` is `https://finance.addventuresinc.com`. It decides whether the login
cookie is marked Secure, and it is embedded in the QuickBooks `.qwc` file we
hand to the IT company — so the `.qwc` must be generated from this value, not
from the onrender.com address.

**Sample data flags are all off and the app is empty of samples** as of
2026-09-08 — Zack is loading real documents.

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

## Still open

- **JobNimbus API key** — site-wide, expected from Zack. Then run
  `scripts/jobnimbus_probe.py <real job number>` once to pin the field names.
- Do subs bill **AIA-style (G702/G703)** or a flat percentage of one contract
  number? This gates sub email ingestion.
- Customer-side change orders — specified in `docs/subcontractor-check-requests.md`,
  not built.
- Retainage on subs and lien waivers — raised, never confirmed.
- Custom domain `finance.addventuresinc.com` — not set up.
- **Never yet verified: a real vendor quote checked against a real vendor
  invoice.** Everything so far has run on synthetic documents.

## Before pushing

```
cd /home/user/finance-automation && .venv/bin/python -m pytest -q    # 637 passing as of 2026-09-08
```
