# Programme 3 — Subcontractor check requests

**Status: built and then corrected, 5 Sep 2026.**

The first version conflated two things Zack keeps separate: *"It has to be two
things. The one part should be exactly like the vendor invoicing. And the second
part should be strictly for checks. Not all checks are for subs — there's
multiple reasons for check request, permits yes for jobs but not for subs."*

So there are two programmes, not one:

- **Subcontractor invoices** — an ordinary Invoice, through the ordinary
  pipeline, matched line by line against the subcontract. The only addition is
  the ceiling check, since a contract caps what a sub can bill in total and a
  material quote never does.
- **Check requests** — any payment request: a permit, a deposit, a fee, a
  reimbursement. No contract, no draw arithmetic. **Always a job**, though —
  *"Everything will have a job. Job numbers are how we do everything."*

Still NOT built: subcontracts and sub invoices arriving by email with a document
type of their own. A contract is marked from a quote already on the job for now.
Question 2 below remains the open fork.

---

## What was asked for

> "Another portion of this whole website should be a check request. So we get
> the same process for subs. Subcontractors will send their main quote in. We
> should have that also sent to the website. And it should be recognized that
> it's a different type of quote. It gets logged as a sub underneath that same
> job number. Subs also get progress payments. So when their progress invoice
> comes in, that should be sent the same way. But when finance goes to look at
> it, it should be check requests. Where they need some sort of verification
> that their request aren't exceeding the main quote invoice. Their homepage
> should have a small dash as well on top like subs total price approved checks
> to date and it's total remaining balance. And then final of any overages.
> Because sometimes subs do get more than they quoted. For extras and what not"

In pieces:

1. A subcontractor's **main quote** arrives the same way everything else does —
   forwarded to the finance mailbox, or uploaded.
2. It is **recognised as a different kind of document**: a subcontract, not a
   material quote.
3. It is **filed under the same job number**, as a sub on that job.
4. Subs bill in **progress payments**, which arrive the same way.
5. Finance sees those as **check requests**, not invoices.
6. Each request is checked so that **cumulative requests do not exceed the
   contract**.
7. A **dashboard strip** on the sub: contract value, checks approved to date,
   remaining balance, overages.
8. **Overages are legitimate.** Subs do get more than they quoted, for extras.
   The system reports them; it does not treat them as errors.

---

## Why this is a genuinely different programme, not a setting

The vendor side compares a material invoice to a material quote **line by
line** — SKU, quantity, unit price, extended. That is the whole engine, and
almost none of it applies here.

A subcontractor's progress bill has no line items to match. It says *"30% of
the roof is complete"* or carries a schedule of values with a percentage against
each phase. There is no unit price to check, because the number being billed is
not a price — it is a **fraction of a price already agreed**.

So the comparison changes shape entirely:

| | Material vendor | Subcontractor |
|---|---|---|
| The document | Invoice with line items | Check request / progress bill |
| The check | Is each line priced as quoted? | Does billed-to-date exceed the contract? |
| The arithmetic | Per line, per invoice | **Cumulative, across every request to date** |
| The unit | One invoice against one quote | One sub against one contract, over months |
| Overage means | Probably an error | Often extras, and legitimate |

**The cumulative check is the feature.** Any single request can look perfectly
reasonable and the seventh one still takes the sub past their contract. That is
the same insight as the job roll-up in `app/jobsummary.py` — the failure is only
visible when you add it up — except here it is not a safety net on top of the
real check, it *is* the real check.

## What can be reused as-is

Most of the pipeline, which is the argument for having built it properly:

- **Ingestion** — mailbox polling, scan splitting, job-number filing, the
  kick-back email when no job number is given. All of it applies unchanged.
- **`app/trust.py`** — provenance screening. A fake check request from a sub who
  does not exist is the same attack as a fake invoice, and the same signals
  catch it. Arguably more valuable here: check requests are for larger amounts.
- **`app/approval.py`** — tiers, blockers, the audit trail of who approved what.
  The routing rules change; the machinery does not.
- **Change orders** — already modelled, already raise the authorised ceiling on
  a job. This is exactly the mechanism extras need.
- **`app/cashflow.py`** — an approved check request is a payable in the 13-week
  forecast, and the phasing work already done applies to it.

## What is new

- A **subcontract** document type, and telling it apart from a material quote.
- A **cumulative billing ledger** per sub: contract, change orders, each request,
  approved to date, remaining.
- The **check request** view itself, which is not the marked-up-invoice view.
- **Retainage on the payable side** (see below).

---

## Also pinned here: customer-side change orders

Zack, correcting the vendor change-order work: *"My change order was more like
us to the community. Increasing our price."*

That is a different object from the `ChangeOrder` this app models, which is a
supplier's authorisation to charge us above their quoted price. A customer
change order raises **the contract value we bill out** — it belongs with
progress billings on the cash flow report, not anywhere near invoice matching.

Concretely, when it gets built:

- It increases the job's contract value, which is what draws are billed against.
- It flows into the 13-week forecast as additional receivable value, phased the
  same way a draw is (billed in week N, collected N + lag, less retainage).
- On the sub side it is the mirror image: a sub's extras raise what they may
  bill, which is question 6 below.

Nothing in the vendor programme needs to know about it, which is precisely why
keeping the two apart matters.

## Questions to settle before building

These are real forks, not details. Each one changes the data model.

**1. How does the system know a subcontract from a material quote?**
Likely signals: labour rather than parts, a scope of work rather than a line
list, a payment schedule, retainage and insurance clauses, W-9 or COI
references. Claude can probably read this reliably — but on a first pass it
should **propose and let a person confirm**, the same way `quote_match == "sole"`
is surfaced rather than assumed. Filing a subcontract as a material quote would
price every one of that sub's bills against nothing.

**2. Schedule of values, or a single contract number? — STILL OPEN**
If subs bill AIA-style (G702/G703) against a schedule of values, the ledger is
per phase and the check is per line of the schedule. If they bill a flat
percentage of one number, it is far simpler. **Ask Zack which his subs actually
send** — this is the single biggest fork in the design, and guessing wrong means
rebuilding it.

**3. Does Add Ventures hold retainage on its subs?**
Almost certainly yes, and this is worth naming: in the competitive research I
raised retainage as a gap and Zack correctly said it did not apply — a condo
holds retainage on Add Ventures, not Add Ventures on a material supplier. That
is true for **material suppliers**. It is not true for **subs**, where holding
retainage is standard. So retainage comes back here, on the payable side, and
the forecast needs to know that an approved check request pays out less than its
face value until closeout.

**4. Lien waivers — same story.**
Dismissed for material suppliers, correct at the time. For subs on a condo
project they are usually a condition of payment: conditional waiver with the
request, unconditional once the check clears. Worth asking whether Add Ventures
collects them today, and if so whether a missing waiver should block a check
request the way a missing receipt blocks an invoice.

**5. What is the receiving leg?**
For materials it is a delivery confirmation. For a sub it is somebody confirming
the work claimed is actually complete — a PM signing off "yes, 30% is done."
Without that, a check request is a claim with nothing behind it. `Receipt`
already has a `work_completion` kind, so the model is there.

**6. Does an overage need a change order before it can be paid?**
On the vendor side it does. Same rule here is the obvious default, and it fits
what Zack said — extras are legitimate, they just need to be written down. Worth
confirming rather than assuming, since sub extras may be agreed verbally on site
and papered later.

---

## The dashboard strip, as described

Per sub, on the job:

| | |
|---|---|
| **Contract** | The subcontract total, plus approved change orders |
| **Approved to date** | Every check request signed off so far |
| **Remaining** | Contract − approved |
| **Overage** | Approved beyond contract + change orders, when positive |

Same shape as the job scorecard already shipped, and for the same reason: five
numbers that answer "where are we with this sub" without opening anything.

---

## The gate

Zack: *"Once we get the vendor one running."* Concretely, that means:

- A **real vendor quote checked against a real vendor invoice** — still the last
  unproven link. Everything so far has been checked against synthetic documents.
- The mailbox carrying **real forwarded documents** rather than test mail.
  (Connected 4 Sep 2026; nothing real has been through it yet.)
- `LOAD_SAMPLES` off, and job 260000 holding real work rather than the sample.

Until those hold, building this would be building a second thing on an
unverified first thing.
