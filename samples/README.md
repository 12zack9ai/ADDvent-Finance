# Sample documents — job 260000

A realistic roofing job, laid out to match a real New Castle Building Products
quote: same header block, same QUANTITY / UOM / ITEM-DESCRIPTION / PRICE-UOM /
AMOUNT columns, same packaging notes in the descriptions.

Upload them in this order. Tick **"Make this the master quote for the job"** on
the first one, and put `260000` in the job number box each time.

| File | What it shows |
|---|---|
| `01-QUOTE-…891004` | The master quote. 12 roofing lines, $21,176.05. |
| `02-INVOICE-…114872` | First delivery. Two lines exactly as quoted, one billed $2.25/RL over, one $1.25/BN under. |
| `03-INVOICE-…119045` | Second delivery. Shingles $2.25/SQ over, **plus two items never quoted** — a ridge adapter and sealant. |
| `04-INVOICE-…126310` | Third delivery. Nails 5% over, ridge cap under, **plus a $185 delivery charge the quote never mentioned**. |

Between them they exercise every verdict the marked-up copy can show — red,
green, gold and grey — and two cases worth understanding:

**The packaging trap.** `BB-SFM558` is billed **12 PK at $155.00/BX**. Quantity
is counted in packs, price is quoted per box, and five packs make a box. Read
naively that is 12 × $155 = $1,860 against a printed $372, which would look like
a catastrophic error on every invoice carrying that item. The engine compares
the effective unit price instead and correctly calls it an exact match.

**Nothing to compare is not the same as correct.** The unquoted lines on
invoices 2 and 3 are marked grey and called out in a banner. They are not
flagged as wrong - an extra item or a delivery charge is often legitimate - but
they are never quietly counted as fine, because no quoted price existed to check
them against.

Regenerate them with `python scripts/make_sample_docs.py`.
