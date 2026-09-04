# Email to IT — hosting request

DNS is handled separately, so it is not asked for here.

Copy from the line below. Replace the bracketed bits.

---

**Subject:** Hosting request — internal finance app at finance.adventuresinc.com

Hi [name],

We've had an internal tool built for the accounting side of the business. It
reads vendor quotes and invoices, checks that what we're billed matches what we
were quoted, and routes anything that doesn't line up for approval. It's ready to
deploy and I'd like to get it onto a server this week.

It's a self-contained Python web application. Full install instructions are in
the repository at `deploy/DEPLOY.md` — start to finish, roughly 30 minutes. I can
give you access to the repo.

**What I'm asking for:**

**1. A Linux VM to run it on**

| | |
|---|---|
| OS | Debian or Ubuntu (current LTS) |
| Size | 2 vCPU, 4 GB RAM, 40 GB disk — it's a light workload |
| Packages | `python3` (3.11+), `python3-venv`, `nginx`, `chromium`, `certbot` |
| Outbound | HTTPS to `api.anthropic.com` — required, the app can't function without it |
| Inbound | 80 and 443, for staff to reach the site |
| Runs as | A dedicated non-login service account, under systemd |

**2. Nightly backup of one directory**

`/var/lib/finance-automation` — the database, every uploaded document, and the
approval audit trail. That single directory is the entire state of the system;
everything else can be rebuilt from the repository in twenty minutes. I'd like
a restore tested once rather than assumed.

**A few things you'll probably want to know:**

- **Data:** vendor quotes and invoices, which include pricing and occasionally
  vendor bank details. Documents are stored encrypted at rest and the site sits
  behind a password. Nothing is public — please keep it out of search engines
  (the supplied nginx config sets `X-Robots-Tag: noindex`).
- **External services:** the app sends document images to Anthropic's API to read
  them. That's the only outbound dependency.
- **Maintenance:** standard OS patching. The app updates with a `git pull` and a
  service restart; no database migrations to hand-run.
- **Access:** I'll supply two secrets to go in the config file — an API key and
  a shared password for staff. They shouldn't be stored anywhere else.

**Coming shortly after — please start this now, it has the longest lead time:**

We want the app to read invoices from a dedicated mailbox automatically. That
needs an **Entra ID app registration** with **application** permission
`Mail.ReadWrite` on one mailbox only (something like `ap-inbox@adventuresinc.com`),
with admin consent granted. Ideally scoped with an `ApplicationAccessPolicy` so it
can only ever read that one mailbox and nothing else.

We don't need it to launch — the app works by uploading documents in the
meantime — but approval typically takes longer than the rest of this combined, so
I'd rather it was in motion. The exact steps are documented in the repo at
`app/mailbox.py`.

**One question, for later:** where does our QuickBooks company file actually live,
and which version and edition are we on? There's a second phase that would read
from it for cash-flow reporting. Nothing needed now — just want to know what we're
working with.

Happy to jump on a call if that's quicker than email.

Thanks,
[your name]

---

## Notes before you send

- **Delete the mailbox section** if you'd rather not raise email yet. The rest
  stands on its own. I'd keep it — that approval is the long pole and there's no
  cost to starting it.
- **If they say no to hosting it**, the fallback is a managed platform (Render or
  Railway, roughly $50–100/month) which needs nothing from them. Worth knowing
  before the conversation so a "no" doesn't stall things.
- **They will ask what happens if it breaks.** Honest answer: it's a read-and-check
  tool, not a system of record. If it stops, invoices get checked by hand exactly
  as they are today, and nothing is lost.
