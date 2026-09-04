# Mailbox — settled

Protected Harbor set this up on 4 Sep 2026, ticket #2025897.

| | |
|---|---|
| Address | `aifinance@addventuresinc.com` |
| Platform | **Their own mail server** — not Microsoft 365, so a password works |
| IMAP | `mail.protectedharborinc.com` port **993**, SSL/TLS |
| SMTP | `mail.protectedharborinc.com` port **587** (STARTTLS) or 465 (SSL/TLS) |
| Password | In their vault; it is the real mailbox password, not a temporary one |
| QuickBooks | **Desktop** — confirmed, so there is no cloud API and the cash flow runs from aging exports |

Everything above is set on the service. The only value not in the repository is
`IMAP_PASSWORD`, which is entered in Render directly.

**Their own mail server was the good answer.** On Microsoft 365 a password
cannot connect to IMAP at all — Microsoft retired basic authentication — and it
fails in a way indistinguishable from a wrong password. The Graph backend in
`app/mailbox.py` exists for that case and is not needed.

The original request below is kept for the next mailbox, and for the Graph
route if the business ever moves to Microsoft 365.

---

## The one fork that matters

How the mailbox is hosted decides which of two connectors we use, and asking
for the wrong thing costs a round trip. So ask this question first:

**"Is the mailbox on Microsoft 365 / Exchange, or on our own web hosting?"**

| Answer | What we need | Why |
|---|---|---|
| **Our own hosting** (cPanel/Plesk) | IMAP host, port 993, full address, password | Works immediately. Nothing to register, nobody to approve it. |
| **Microsoft 365** | An Entra ID app registration (details below) | Microsoft disabled basic authentication for IMAP. A username and password alone will not connect, no matter how many times it is retyped. |

If the answer is Microsoft 365, **asking for "IMAP access" will produce a
mailbox that cannot be connected to.** Ask for the app registration instead.

---

## Copy from below. Replace the bracketed bits.

**Subject:** Mailbox request — finance document inbox

Hi [name],

We've got an internal tool running for the accounting side of the business. It
reads vendor quotes and invoices, checks that what we're billed matches what we
were quoted, and flags anything that doesn't line up. It's already hosted, so
there's no server needed from you.

The one thing I need is a mailbox it can read, so staff and vendors can forward
documents straight to it rather than uploading them by hand.

**The mailbox**

| | |
|---|---|
| Address | `aifinance@addventuresinc.com` |
| Used by | The application only. No person will sign into it. |
| Contents | Vendor quotes and invoices sent to us as PDF attachments |
| Volume | Low — roughly 100–500 messages a month |

**What I need back depends on where it lives:**

*If it's on our own web hosting (cPanel/Plesk):*

- IMAP hostname (usually `mail.addventuresinc.com` or the bare domain)
- Port — 993 with SSL
- The full email address, and its password

That's everything. Nothing else to configure.

*If it's on Microsoft 365 / Exchange:*

Basic authentication for IMAP was retired by Microsoft, so a password won't
work. I need an **Entra ID app registration** instead:

- An app registration with **application permissions** (not delegated):
  `Mail.ReadWrite` and `Mail.Send`
- **Admin consent granted** on those permissions
- The **tenant ID**, **client ID**, and a **client secret**
- An **application access policy** restricting that registration to this one
  mailbox — so it can read the finance inbox and nothing else in the tenant

That last point is the important one. Without it the registration can read every
mailbox in the organisation, which is far more access than this needs.

**A couple of things you'll probably want to know:**

- The app reads messages, saves the attachments, and moves what it has processed
  into a `Processed` folder. It doesn't delete anything and doesn't send mail on
  anyone's behalf.
- Attachments are sent to Anthropic's API to be read. That and the mailbox are
  its only outbound connections.
- The credentials live in the hosting platform's encrypted environment settings,
  not in a file and not in the code repository.

Happy to jump on a call if that's quicker.

Thanks,
[you]

---

## When the answer comes back

Nothing needs redeploying. The values go into Render → Environment:

**Own hosting:**

```
MAIL_ENABLED=true
IMAP_HOST=<what they gave you>
IMAP_PORT=993
IMAP_USER=<the FULL address, including @addventuresinc.com>
IMAP_PASSWORD=<the password>
```

**Microsoft 365:**

```
MAIL_ENABLED=true
MS_TENANT_ID=<tenant id>
MS_CLIENT_ID=<client id>
MS_CLIENT_SECRET=<client secret>
MS_MAILBOX=<the full address>
```

Then confirm it before trusting it. This files nothing and marks nothing read:

```bash
.venv/bin/python scripts/test_mail.py
```

After that, watch `/healthz`. A configured mailbox that has gone quiet reports
`"stale": true` — the site keeps serving pages perfectly while receiving
nothing, which is how a background poller fails without anyone noticing.
