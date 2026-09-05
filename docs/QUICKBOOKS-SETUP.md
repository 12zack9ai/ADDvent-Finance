# Connecting ADDvent Finance to QuickBooks Desktop

**For whoever runs the Windows machine and the QuickBooks company file.**
Everything on our side is built and waiting. This is the part that has to
happen on yours, and it is a one-time setup — about an hour, most of it
provisioning a VM.

---

## The one thing to understand first

**QuickBooks Desktop has no cloud API.** Not a limited one, not a paid one —
none. Intuit's developer pages that look like REST endpoints (`.../billadd`)
are *qbXML message schemas* for a Windows COM SDK. They are not URLs and
nothing can call them over the internet.

The only supported way in from outside the building is the **QuickBooks Web
Connector**: a small free Intuit program that runs on Windows next to the
company file. It **calls out** to a web address on a schedule and asks "have
you got anything for me?" We answer with one instruction at a time; it carries
each one to QuickBooks and brings the answer back.

So the direction is the opposite of what people expect. **We never call
QuickBooks. QuickBooks calls us.** Nothing needs to be opened up to let us in;
the Windows machine needs to be able to make outbound HTTPS calls, which it
already can.

---

## What we need from you

### 1. A Windows machine that stays logged in

| | |
|---|---|
| **OS** | Windows 10/11 Pro or Windows Server |
| **Spec** | 2 vCPU, 4 GB RAM — a VM on existing hardware is fine |
| **Network** | Same LAN as the QuickBooks company file. **Outbound HTTPS only.** No inbound ports, no port forwarding, no firewall changes. |
| **QuickBooks** | Installed, with its own dedicated QuickBooks user account for this integration |
| **Critical** | It must be **always on and always logged in.** |

That last row is the awkward one and we would rather flag it than have it
discovered later. **The Web Connector cannot run as a Windows Service.** It
needs an interactive desktop session. If the machine reboots and lands on the
login screen, the sync stops silently — nothing errors, it simply never runs
again until somebody logs in.

The usual answer is auto-logon, configured with Sysinternals `Autologon`
(which stores the credential encrypted in LSA rather than in plaintext in the
registry), plus:

- the Web Connector launched by a Scheduled Task with an **At log on** trigger,
  set to restart on failure
- the screen set to lock immediately after logon
- Windows Update pinned to a fixed maintenance window, so it does not reboot
  the machine at 2pm on a Tuesday

**We know a permanently logged-in console is a thing you may have a policy
about.** The mitigations we would suggest: a dedicated low-privilege account
used for nothing else, no RDP from outside, and the screen locked. If the
answer is still no, say so early — there is a cloud-hosted alternative
(below) and it changes our plan, not yours.

### 2. Someone to approve it once, inside QuickBooks

The first time the connector runs, QuickBooks shows a dialog asking whether
this application may access the company file. It has to be answered **by an
administrator, with the company file open, in single-user mode**, choosing
*"Yes, always allow access even if QuickBooks is not running"*.

That is the only time anyone has to be present. After that it is unattended.

### 3. Ten minutes of your time to answer three questions

- Where does the company file physically live, and what QuickBooks
  year/edition is it?
- Can you provision the VM above, and at what cost?
- What is your policy on third-party software with SDK access to the company
  file?

---

## The setup, step by step

1. **We set a password.** On our side: `QBWC_PASSWORD` and
   `QUICKBOOKS_ENABLED=true`. We will send you the username and password
   separately from this document.

2. **Download the connection file.** Open
   `https://finance.addventuresinc.com/quickbooks` and click **Download the
   .qwc file**. It is a small XML file naming our address, the username, and
   how often to run. Nothing secret is in it — the password is typed into the
   connector, not stored in the file.

3. **Install the Web Connector** on the Windows machine, if it is not already
   there. It ships with QuickBooks; otherwise it is a free download from
   Intuit.

4. **Open the .qwc file** on that machine. The Web Connector opens and offers
   to add the application. Say yes.

5. **Type the password** into the connector's password box when prompted, and
   let it save it.

6. **Open the company file in QuickBooks** and press **Update Selected** in the
   connector once, by hand. QuickBooks shows the certificate/permission dialog
   described above. Approve it as an administrator, choosing *always allow*.

7. **Confirm it worked.** Back on
   `https://finance.addventuresinc.com/quickbooks` you should see the company
   file name, a count of customers and jobs, and a count of invoices. If the
   page still says "Not yet", the connector's own log window says why.

From then on it runs itself, every 30 minutes.

---

## What it actually does

**Reads, on every run:**

| | |
|---|---|
| The company file name | So we notice immediately if it is ever pointed at the wrong file |
| Customers and jobs | The `Customer:Job` list, so our job numbers can be matched to yours |
| Customer invoices | Amount and balance remaining, which is where "billed" and "collected" come from |

Only what has **changed since the last run** — never a full pull. A bulk read
holds a lock the people working in the company file feel, and this runs while
they are working.

**Writes: nothing, unless it is switched on.** `QBWC_WRITE_BACK` is off. When
Zack turns it on, the only thing it writes is a vendor bill that a person has
already approved in our system, with its job coding, linked to its purchase
order where one exists. Every write carries an ID we generate, so a retry after
a timeout — which is ordinary with a polling connector — cannot create the
same bill twice.

**It never edits or deletes anything in QuickBooks.** QuickBooks is the book of
record for money. Our copy is a read-only mirror.

---

## How our jobs are matched to yours

By the **six-digit job number in the QuickBooks job name**, and by nothing
else. A job called

    Daul Gardens Condominium Association:269001 Building 4 reroof

matches our job 269001.

We deliberately do **not** match on the name. Two associations can both have a
"Building 4", and filing one job's money against another is not a mistake
anybody would catch from a costing report. A QuickBooks job with no recognisable
number is listed on our status page as unmatched, for a person to sort out.

**If it helps: putting the job number at the front of the job name in
QuickBooks makes every one of them match automatically.** No change to how
anything is billed — just the name.

---

## If the answer to the Windows VM is no

There is a middle path worth knowing about. Several companies (Conductor,
Rutter, Codat) sell a hosted version of exactly this: they run the Windows
agent, we call their REST API. It costs a few hundred dollars a month and
removes the always-logged-in machine from your side entirely.

We have built the direct route because it costs nothing to run and depends on
nobody. If your answer to the VM is no, or slow, or expensive, the hosted route
is a configuration change on our side rather than a rewrite — everything that
talks to QuickBooks here goes through one interface.

---

## Security, in plain terms

- **The only address exposed** is `https://finance.addventuresinc.com/qbwc`.
  It is the one page on our site without a login, because the Web Connector is
  a Windows service with no browser. It is guarded by its own username and
  password, and the *only* thing it can do is hold a sync conversation. There
  is no route from it to a page, a document, or a decision.
- **Nothing is opened on your network.** Outbound HTTPS only.
- **The password is compared in constant time**, so the endpoint gives nothing
  away to somebody guessing at it.
- **We hold a copy of customer invoice totals and balances.** No bank details,
  no payroll, no employee records — we never ask for them.

---

## When something stops

With a polling connector, **silence is ambiguous**: a connector that has
stopped looks exactly like a connector with nothing to do. So check the status
page, which shows when it last called and what it said.

| What you see | Usually means |
|---|---|
| "Not yet" after setup | The password is not set on our side, or `QUICKBOOKS_ENABLED` is off |
| Connector shows an authentication failure | Wrong password typed into the connector |
| Last call is hours old | The machine rebooted and nobody is logged in |
| A step shows "The company file is busy" | Somebody had QuickBooks in single-user mode, or a dialog was left open. It retries on the next run. |
| Connector reports it cannot open the company file | QuickBooks is not running and "always allow" was not chosen at step 6 |

A failed step never loses the run — the cursor only advances when a step
finishes cleanly, so whatever failed is simply asked for again next time.
