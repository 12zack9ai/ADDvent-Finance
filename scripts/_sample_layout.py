"""Generate New Castle-style test documents: one master quote, three invoices.

Layout copied from the real quote Zack photographed - same header block, same
QUANTITY / UOM / ITEM-DESCRIPTION / PRICE-UOM / AMOUNT column order, same
account and branch fields, same "n PC/BX" packaging notes in the descriptions.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pdf import render_html_to_pdf

OUT = Path(__file__).resolve().parent.parent / "samples" / "job-4482"
OUT.mkdir(parents=True, exist_ok=True)

CSS = """
@page { size: letter; margin: 0.45in 0.5in; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 8.4pt; color: #000; margin: 0; }
.logo { font-family: Georgia, 'Times New Roman', serif; font-weight: bold; font-size: 17pt;
        letter-spacing: .5px; line-height: 1; }
.logo .sub { display:block; font-size: 8pt; letter-spacing: 2.4px; font-weight: normal; margin-top: 2px; }
.vend { font-size: 7.6pt; line-height: 1.35; margin-top: 6px; }
.docbox { border: 1.6px solid #000; text-align: center; min-width: 168px; }
.docbox .t { background: #d9d9d9; font-weight: bold; letter-spacing: 3px; font-size: 10pt;
             padding: 3px 0; border-bottom: 1.6px solid #000; }
.docbox .n { padding: 5px 0; font-size: 10.5pt; letter-spacing: .5px; }
table.meta { border-collapse: collapse; width: 100%; }
table.meta td { vertical-align: top; padding: 0; }
.acct { font-size: 7.6pt; line-height: 1.5; }
.acct b { display: inline-block; min-width: 58px; }
.parties { margin-top: 10px; font-size: 7.8pt; line-height: 1.35; }
.parties .lbl { font-weight: bold; }
.band { border: 1px solid #000; border-collapse: collapse; width: 100%; margin-top: 9px;
        font-size: 7.4pt; }
.band td { border: 1px solid #000; padding: 2px 4px; }
.band .k { font-weight: bold; }
table.items { border-collapse: collapse; width: 100%; margin-top: 9px; }
table.items th { background: #d9d9d9; border-top: 1.4px solid #000; border-bottom: 1.4px solid #000;
                 font-size: 7.4pt; letter-spacing: .4px; padding: 3px 4px; text-align: left; }
table.items th.r, table.items td.r { text-align: right; }
table.items td { padding: 4px 4px 5px; vertical-align: top; border-bottom: 1px solid #e8e8e8; }
table.items .sku { font-weight: bold; font-size: 7.8pt; }
table.items .desc { font-size: 7.8pt; }
table.items .note { color: #333; font-size: 7pt; }
.tot { width: 46%; margin-left: auto; margin-top: 10px; border-collapse: collapse; font-size: 8.2pt; }
.tot td { padding: 2.5px 5px; }
.tot td.r { text-align: right; }
.tot tr.grand td { border-top: 1.4px solid #000; font-weight: bold; font-size: 9.4pt; padding-top: 4px; }
.foot { margin-top: 16px; font-size: 7pt; color: #333; border-top: 1px solid #bbb; padding-top: 5px; }
.terms { margin-top: 10px; font-size: 7.2pt; }
"""

def head(kind, number, meta_rows, po, dates, extra_band=""):
    rows = "".join(
        f'<div><b>{k}</b> {v}</div>' for k, v in meta_rows
    )
    band = "".join(
        f'<td class="k">{k}</td><td>{v}</td>' for k, v in dates
    )
    return f"""
<table class="meta"><tr>
  <td style="width:56%">
    <div class="logo">NEW CASTLE<span class="sub">BUILDING PRODUCTS</span></div>
    <div class="vend">New Castle Building Products<br>575 Island Road<br>
      Ramsey, NJ 07446<br>Phone: (201)-252-7880</div>
  </td>
  <td style="width:44%" align="right">
    <div class="docbox"><div class="t">{kind}</div><div class="n">{number}</div></div>
    <div class="acct" style="margin-top:7px; text-align:left; display:inline-block">{rows}</div>
  </td>
</tr></table>

<table class="meta parties"><tr>
  <td style="width:50%"><span class="lbl">Bill To:</span> Add Ventures Construction Svcs<br>
     <span style="padding-left:38px">12 Suffern Road</span><br>
     <span style="padding-left:38px">Hillburn, NY 10931</span></td>
  <td style="width:50%"><span class="lbl">Ship To:</span> Add Ventures Construction Svcs<br>
     <span style="padding-left:40px">118 Ridgeview Terrace</span><br>
     <span style="padding-left:40px">Mahwah, NJ 07430</span></td>
</tr></table>

<div style="margin-top:8px; font-size:7.8pt"><b>PO:</b> {po}</div>
<table class="band"><tr>{band}</tr>{extra_band}</table>
"""

def items_table(lines):
    body = ""
    for l in lines:
        note = f'<div class="note">{l["note"]}</div>' if l.get("note") else ""
        body += (
            f'<tr><td class="r">{l["qty"]}</td><td>{l["uom"]}</td>'
            f'<td><span class="sku">{l["sku"]}</span><br>'
            f'<span class="desc">{l["desc"]}</span>{note}</td>'
            f'<td class="r">{l["price"]}/{l["puom"]}</td>'
            f'<td class="r">{l["amount"]}</td></tr>'
        )
    return f"""
<table class="items">
 <thead><tr><th class="r" style="width:8%">QUANTITY</th><th style="width:7%">UOM</th>
   <th>ITEM/DESCRIPTION</th><th class="r" style="width:15%">PRICE/UOM</th>
   <th class="r" style="width:13%">AMOUNT</th></tr></thead>
 <tbody>{body}</tbody></table>
"""

def totals(rows):
    out = ""
    for label, val, grand in rows:
        cls = ' class="grand"' if grand else ""
        out += f'<tr{cls}><td>{label}</td><td class="r">{val}</td></tr>'
    return f'<table class="tot">{out}</table>'

def page(title, inner):
    return (f'<html><head><meta charset="utf-8"><title>{title}</title>'
            f'<style>{CSS}</style></head><body>{inner}</body></html>')
