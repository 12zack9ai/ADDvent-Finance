import sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _sample_layout import *   # noqa
from app.pdf import render_html_to_pdf

D = Decimal
def money(d): return f"{d:,.2f}"

# 5 packs make a box - the packaging case from Zack's real quote.
PK_PER_BX = D(5)

def amt(qty, price, uom, puom):
    q, p = D(str(qty)), D(str(price))
    if uom == "PK" and puom == "BX":
        return (q / PK_PER_BX * p).quantize(D("0.01"))
    return (q * p).quantize(D("0.01"))

def L(qty, uom, sku, desc, price, puom, note=""):
    a = amt(qty, price, uom, puom)
    return {"qty": qty, "uom": uom, "sku": sku, "desc": desc, "note": note,
            "price": money(D(str(price))), "puom": puom, "amount": money(a), "_a": a}

def sum_lines(lines): return sum((l["_a"] for l in lines), D(0))

ACCT = [("Account:", "ADDVE 0001"), ("Branch:", "01007RM"),
        ("Phone:", "(845)-357-7134"), ("Fax:", "(845)-357-7177")]
PO = "118 ridgeview terrace"

# ----------------------------------------------------------------- MASTER QUOTE
quote_lines = [
    L(96, "SQ", "GAFT3CH",   "GAF TIMBERLINE HDZ CHARCOAL 3 BN/SQ", "118.75", "SQ"),
    L(10, "RL", "GAFTP",     "GAF TIGER PAW UNDERLAYMENT 10 SQ/RL", "187.00", "RL", "48\" X 250', 30 RL/PA"),
    L(32, "RL", "GAFIW2",    "GAF WEATHERWATCH 2 SQ/RL 66.7 LF/RL", "91.25", "RL"),
    L(64, "PC", "GAFASC",    "GAF COBRA SNOW COUNTRY ADVANCED 4 FT/PC", "14.50", "PC", "11 1/2\" x 4', 10 PC/BX"),
    L(10, "BN", "GAFSTART",  "GAF PRO-START 13\"", "57.25", "BN"),
    L(14, "BN", "GAFTHRCH",  "GAF TIMBERTEX H&R CHARCOAL 20 FT/BN", "69.00", "BN"),
    L(104, "EA", "BB-F55OW2","DRIP EDGE F5 1/2 ALUM OPEN FACED ECON WHITE", "9.70", "EA", "10', 50 EA/CT, (#F55OW2)"),
    L(12, "PK", "BB-SFM558", "STEP FLASHING ALUM PREBENT MF 50 EA/PK, 5 PK/BX", "155.00", "BX", "5\" X 5\" X 8\", 250 EA/BX, (#SFM558)"),
    L(7,  "BX", "NRG114-C",  "EG GALVANIZED ROOFING 3D 15° COIL 7.2 M/BX", "44.25", "BX", "1-1/4\""),
    L(2,  "EA", "BB-201-24", "TRIM COIL ALUM ROYAL BROWN/WHITE 1 EA/CT", "138.00", "EA", "24\" X 50', (#201-24)"),
    L(12, "SH", "PWCDX1/2",  "PLYWOOD CDX 1/2\"", "26.50", "SH", "4' X 8'"),
    L(20, "PC", "GAFCOB",    "GAF COBRA RIDGE VENT 4 FT/PC", "11.75", "PC"),
]
sub = sum_lines(quote_lines)
inner = (head("QUOTE", "07RM0002891004", ACCT, PO,
              [("Exp Delv Date:", "09/08/26"), ("Activation Date:", "09/08/26"), ("Close Date:", "09/23/26")],
              '<tr><td class="k">Job:</td><td>260000</td><td class="k">Ship Via:</td><td>Truck</td>'
              '<td class="k">Quoted By:</td><td>pcricelli</td></tr>')
         + items_table(quote_lines)
         + totals([("Subtotal", money(sub), False), ("Freight", "0.00", False),
                   ("Tax", "0.00", False), ("TOTAL", money(sub), True)])
         + '<div class="terms"><b>Terms:</b> Net 30. Quoted prices firm through Close Date. '
           'Material returns subject to restocking. Prices exclude tax unless shown.</div>'
         + '<div class="foot">Page 1 of 1 &nbsp;&nbsp;|&nbsp;&nbsp; Printed: 09/08/26 &nbsp; 09:14:22'
           ' &nbsp;&nbsp;|&nbsp;&nbsp; Agent: Paul Cricelli &nbsp; pcricelli@ncbp.com</div>')
render_html_to_pdf(page("Quote 07RM0002891004", inner), OUT / "01-QUOTE-07RM0002891004.pdf")
print(f"quote      {len(quote_lines):2} lines  total {money(sub)}")

# ----------------------------------------------------------------- INVOICES
def invoice(fname, number, date, lines, freight=D(0), note=""):
    sub = sum_lines(lines)
    tot = sub + freight
    rows = [("Subtotal", money(sub), False)]
    rows.append(("Freight", money(freight), False))
    rows += [("Tax", "0.00", False), ("TOTAL", money(tot), True)]
    inner = (head("INVOICE", number, ACCT, PO,
                  [("Invoice Date:", date), ("Terms:", "Net 30"), ("Due Date:", "10/08/26")],
                  '<tr><td class="k">Job:</td><td>260000</td><td class="k">Ship Via:</td><td>Truck</td>'
                  '<td class="k">Quote Ref:</td><td>07RM0002891004</td></tr>')
             + items_table(lines) + totals(rows)
             + (f'<div class="terms">{note}</div>' if note else "")
             + f'<div class="foot">Page 1 of 1 &nbsp;&nbsp;|&nbsp;&nbsp; Printed: {date}'
               ' &nbsp;&nbsp;|&nbsp;&nbsp; Remit to: New Castle Building Products, 575 Island Road, Ramsey NJ 07446</div>')
    render_html_to_pdf(page(f"Invoice {number}", inner), OUT / fname)
    print(f"{fname[:22]:22} {len(lines):2} lines  total {money(tot)}")
    return tot

# Delivery 1 - mostly as quoted; one item up 2.5%, one down.
inv1 = [
    L(48, "SQ", "GAFT3CH",  "GAF TIMBERLINE HDZ CHARCOAL 3 BN/SQ", "118.75", "SQ"),
    L(5,  "RL", "GAFTP",    "GAF TIGER PAW UNDERLAYMENT 10 SQ/RL", "187.00", "RL"),
    L(16, "RL", "GAFIW2",   "GAF WEATHERWATCH 2 SQ/RL 66.7 LF/RL", "93.50", "RL"),
    L(5,  "BN", "GAFSTART", "GAF PRO-START 13\"", "56.00", "BN"),
]
invoice("02-INVOICE-07RM0003114872.pdf", "07RM0003114872", "09/12/26", inv1)

# Delivery 2 - shingles up 1.9%, plus two items never quoted.
inv2 = [
    L(48, "SQ", "GAFT3CH",   "GAF TIMBERLINE HDZ CHARCOAL 3 BN/SQ", "121.00", "SQ"),
    L(64, "PC", "GAFASC",    "GAF COBRA SNOW COUNTRY ADVANCED 4 FT/PC", "14.50", "PC"),
    L(104,"EA", "BB-F55OW2", "DRIP EDGE F5 1/2 ALUM OPEN FACED ECON WHITE", "9.70", "EA"),
    L(18, "PC", "GAFRA20",   "GAF COBRA RIDGE ADAPTER 20 PC/BX", "9.25", "PC"),
    L(24, "EA", "NP1-10OZ",  "NP1 POLYURETHANE SEALANT 10 OZ BRONZE", "8.95", "EA"),
]
invoice("03-INVOICE-07RM0003119045.pdf", "07RM0003119045", "09/18/26", inv2)

# Delivery 3 - packaging case billed correctly, one item up 5%, one down, plus freight.
inv3 = [
    L(12, "PK", "BB-SFM558", "STEP FLASHING ALUM PREBENT MF 50 EA/PK, 5 PK/BX", "155.00", "BX", "5\" X 5\" X 8\", 250 EA/BX"),
    L(7,  "BX", "NRG114-C",  "EG GALVANIZED ROOFING 3D 15° COIL 7.2 M/BX", "46.50", "BX"),
    L(12, "SH", "PWCDX1/2",  "PLYWOOD CDX 1/2\"", "26.50", "SH"),
    L(14, "BN", "GAFTHRCH",  "GAF TIMBERTEX H&R CHARCOAL 20 FT/BN", "67.50", "BN"),
    L(2,  "EA", "BB-201-24", "TRIM COIL ALUM ROYAL BROWN/WHITE 1 EA/CT", "138.00", "EA"),
    L(20, "PC", "GAFCOB",    "GAF COBRA RIDGE VENT 4 FT/PC", "11.75", "PC"),
]
invoice("04-INVOICE-07RM0003126310.pdf", "07RM0003126310", "09/24/26", inv3,
        freight=D("185.00"), note="<b>Note:</b> Delivery charge applies to jobsite drops outside standard route.")
