"""Build the PDF test pack: a complete practice data room for a made-up company, Orion Analytics Ltd.

    python scripts/make_pdf_testpack.py            # writes data/testpack/G_full_data_room_pdf/Orion Analytics/
    python scripts/make_pdf_testpack.py --deals    # and copies it into the deals folder (DEALS_DIR or ~/Deals)

Each document is written as HTML and printed to a real PDF by headless Edge or Chrome, so it has selectable text,
tables and page layout like a real data room file. One document is printed as an image only, to stand in for a scan.
Every problem is planted on purpose; _README.txt lists what each document should raise. Nothing here is real.
"""
import argparse
import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "testpack" / "G_full_data_room_pdf" / "Orion Analytics"
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
STYLE = """
@page { size: A4; margin: 22mm 20mm; }
body { font: 10.5pt/1.55 Georgia, 'Times New Roman', serif; color: #111; }
h1 { font: 700 17pt Arial, sans-serif; margin: 0 0 4pt; } h2 { font: 700 12pt Arial, sans-serif; margin: 16pt 0 4pt; }
.meta { font: 9pt Arial, sans-serif; color: #555; margin-bottom: 14pt; border-bottom: 1px solid #999; padding-bottom: 6pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font: 9.5pt Arial, sans-serif; }
th, td { border: 1px solid #888; padding: 4pt 6pt; text-align: left; } td.n, th.n { text-align: right; }
th { background: #eee; } .sig { margin-top: 26pt; font: 9.5pt Arial, sans-serif; }
.foot { margin-top: 24pt; font: 8pt Arial, sans-serif; color: #777; }
"""
PRACTICE = "PRACTICE DOCUMENT for testing Fine Print. Orion Analytics Ltd is not a real company."


def page(title: str, meta: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style></head><body><h1>{html.escape(title)}</h1><div class='meta'>{meta}</div>"
            f"{body}<div class='foot'>{PRACTICE}</div></body></html>")


def table(head, rows, numeric=()):
    th = "".join(f"<th class='{'n' if i in numeric else ''}'>{html.escape(h)}</th>" for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f"<td class='{'n' if i in numeric else ''}'>{html.escape(str(c))}</td>"
                                   for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{trs}</table>"


DOCS = {
    "01 Financial/Confidential Information Memorandum.pdf": page(
        "Orion Analytics Ltd: Confidential Information Memorandum", "Prepared by the seller's adviser, September 2025",
        "<h2>1. The business</h2><p>Orion Analytics Ltd sells subscription analytics software to retailers. Revenue for "
        "FY2024 was $24.6 million, up 31% on FY2023. Management believes growth will exceed 30% a year for the next "
        "three years, and the run-rate revenue at September 2025 is $29.0 million on a pro forma basis.</p>"
        "<h2>2. Adjusted EBITDA bridge, FY2024</h2>"
        "<p>1. Base Operating EBITDA: $5,200,000<br>"
        "2. Litigation Legal Fees: +$900,000 (Non-recurring patent dispute, now settled)<br>"
        "3. ERP Software Migration: +$650,000 (One-off move to a new ERP platform)<br>"
        "4. Management Restructuring &amp; Severance: +$400,000 (One-time departure of the former CFO)</p>"
        "<p>Adjusted EBITDA: $7,150,000</p>"
        "<p>These items are non-recurring and should be added back for valuation purposes.</p>"
        "<h2>3. Customers</h2><p>Orion serves 140 retailers. Its largest customer, Helios Retail Group, has been a "
        "customer since 2019 under the Master Services Agreement in the data room.</p>"
        "<h2>4. Risks</h2><p>The company is not aware of any material litigation.</p>"),

    "01 Financial/Audited Financial Statements FY2024.pdf": page(
        "Orion Analytics Ltd: Audited Financial Statements, year ended 31 December 2024",
        "Auditor: Marlow &amp; Finch LLP. Signed 28 March 2025",
        "<h2>Independent auditor's report (extract)</h2><p>We draw attention to Note 14, which indicates that the company "
        "breached its leverage covenant on 30 September 2024 and that a waiver has not yet been obtained. These conditions "
        "indicate that a material uncertainty exists that may cast significant doubt on the company's ability to continue "
        "as a going concern. Our opinion is not modified in respect of this matter.</p>"
        "<h2>Income statement (USD thousands)</h2>"
        + table(["Line", "FY2024", "FY2023"], [
            ["Product revenue", "14,200", "11,300"], ["Services revenue", "8,700", "6,500"],
            ["Total revenue", "23,900", "17,800"], ["Cost of sales", "(9,100)", "(7,000)"],
            ["Operating expenses", "(10,600)", "(8,400)"], ["Operating profit", "3,200", "2,400"]], numeric=(1, 2))
        + "<p>Revenue for FY2024 was $22.9 million.</p>"
        "<h2>Note 14: Borrowings</h2><p>The term loan of $18.0 million carries a leverage covenant of 3.0x. At 30 "
        "September 2024 leverage was 3.4x, a covenant breach. The lender has reserved its rights.</p>"
        "<h2>Note 19: Contingent liabilities</h2><p>A former distributor has issued proceedings claiming $2.1 million "
        "for wrongful termination. The directors intend to defend the claim.</p>"),

    "01 Financial/Management Accounts and Ledger.pdf": page(
        "Orion Analytics Ltd: Management Accounts, Ledger Extract", "Finance team, unaudited",
        "<p>Table 1: Legal and professional fees</p>"
        "<p>FY22 Legal Fees: $880,000<br>FY23 Legal Fees: $910,000<br>FY24 Legal Fees: $900,000</p>"
        "<p>Audit Note 7: Outside counsel is retained on an annual engagement covering ongoing IP matters.</p>"
        "<p>Note 3: Severance of $400,000 paid in May 2024 on the departure of the former CFO.</p>"
        "<p>Table 2: Software and systems</p>"
        "<p>FY23 ERP Maintenance: $620,000<br>FY24 ERP Maintenance: $650,000</p>"
        "<p>GL Account 6420: Ongoing recurring ERP subscription and maintenance, billed annually.</p>"),

    "02 Commercial/Customer ARR Schedule.pdf": page(
        "Orion Analytics Ltd: Customer Annual Recurring Revenue, FY2024", "Exported from the billing system",
        "<p>Customer annual recurring revenue:</p><p>"
        "Helios Retail Group: $9,100,000<br>Northgate Stores: $2,400,000<br>Brightline Foods: $1,900,000<br>"
        "Cobalt Home: $1,300,000<br>Remaining SMB accounts: $8,200,000<br>Total ARR: $22,900,000</p>"
        "<p>Gross revenue churn in FY2024 was 18%. Two pilots with Avalon Markets are included in ARR.</p>"),

    "02 Commercial/Master Services Agreement - Helios Retail.pdf": page(
        "Master Services Agreement", "Between Orion Analytics Ltd (Supplier) and Helios Retail Group plc (Customer)",
        "<h2>8. Term and termination</h2><p>8.1 This Agreement renews automatically for successive three-year terms. "
        "8.2 The Customer may terminate this Agreement for convenience on 90 days' notice. 8.3 The Customer may terminate "
        "this Agreement immediately on a change of control of the Supplier.</p>"
        "<h2>9. Exclusivity and pricing</h2><p>9.1 The Supplier shall not provide analytics services to any competitor of "
        "the Customer in the United Kingdom. 9.2 The Supplier warrants that the Customer receives most favoured nation "
        "pricing, no less favourable than any other customer.</p>"
        "<h2>12. Liability</h2><p>12.1 The Supplier's liability for data protection breaches and for breach of "
        "confidentiality is unlimited.</p>"
        "<div class='sig'>Signed for the Supplier: ______________________ Date: [TBD]<br><br>"
        "Signed for the Customer: ______________________ Date: [TBD]</div>"),

    "03 Legal/Litigation Schedule.pdf": page(
        "Orion Analytics Ltd: Disputes and Claims Schedule", "Prepared by the company secretary",
        table(["Matter", "Status", "Amount claimed"], [
            ["Former distributor: wrongful termination", "Proceedings issued, defence filed", "$2,100,000"],
            ["Patent infringement claim by DataLoom Inc", "Pending; settlement talks", "Not quantified"],
            ["Information Commissioner investigation into 2023 data breach", "Open", "Possible fine"]], numeric=(2,))
        + "<p>The company considers none of these matters material.</p>"),

    "03 Legal/Supplier Agreement - Single Source.pdf": page(
        "Hosting and Data Supply Agreement", "Between Nimbus Cloud Ltd (Supplier) and Orion Analytics Ltd",
        "<p>1. Nimbus Cloud is the sole supplier of hosting and retail data feeds to Orion. There is no alternative "
        "supplier and no exit plan.</p><p>2. Orion commits to a minimum annual spend of $1,500,000 on a take-or-pay "
        "basis for five years.</p><p>3. Nimbus may terminate on a change of control of Orion.</p>"),

    "04 People/Key Employees and HR Summary.pdf": page(
        "Orion Analytics Ltd: People Summary", "HR, August 2025",
        "<p>Headcount 162. Voluntary attrition in engineering was 28% in FY2024.</p>"
        "<p>The CTO, Priya Raman, wrote the core platform and is not bound by a non-compete. She has said she intends "
        "to leave after the sale.</p><p>Change of control bonuses totalling $1,250,000 are payable to six managers on "
        "completion.</p><p>Forty developers are engaged as independent contractors through personal service "
        "companies; the tax authority has asked about their classification.</p><p>An employment tribunal claim for "
        "unfair dismissal is pending.</p>"),

    "05 Tax/Tax Authority Letter - Open Enquiry.pdf": page(
        "Letter from the tax authority", "To: The Directors, Orion Analytics Ltd. 14 July 2025",
        "<p>We are opening an enquiry into the company's corporation tax returns for 2021 to 2023, in particular "
        "transfer pricing on charges from Orion Analytics Inc (US).</p><p>Our initial view is that additional tax of "
        "$1,400,000 plus interest and penalties may be due. Please provide the transfer pricing documentation within "
        "30 days.</p><p>The company has also not registered for VAT in Germany despite sales there since 2022.</p>"),

    "06 Technology/IT Security Assessment.pdf": page(
        "IT Security Assessment", "Independent review by Sentinel Cyber, June 2025",
        "<p>Key findings:</p><p>1. In March 2023 a data breach exposed 48,000 customer records; regulators were "
        "notified late.</p><p>2. Production databases run on Windows Server 2012, which is end of life and no longer "
        "supported.</p><p>3. Multi-factor authentication is not enforced for administrators.</p><p>4. The analytics "
        "engine includes a GPL-licensed component that is distributed to customers.</p><p>5. There is no tested "
        "disaster recovery plan; backups have never been restored.</p>"),

    "07 Property and ESG/Office Lease.pdf": page(
        "Lease of Unit 4, Riverside Park", "Landlord: Riverside Estates Ltd. Tenant: Orion Analytics Ltd",
        "<p>Term: five years from 1 April 2021, expiring 31 March 2026. There is no option to renew.</p>"
        "<p>Assignment and change of control require the Landlord's prior written consent.</p>"
        "<p>A Phase I environmental survey identified potential soil contamination from the site's former use as a "
        "fuel depot; no Phase II survey has been carried out.</p>"),

    "07 Property and ESG/Insurance Certificate.pdf": page(
        "Certificate of Insurance", "Insurer: Harbour Mutual",
        "<p>Policyholder: Orion Analytics Ltd. Cover: cyber liability and professional indemnity, limit $5,000,000.</p>"
        "<p>This certificate is valid until 31 March 2025.</p>"),

    "07 Property and ESG/Supplier Code and ESG Statement.pdf": page(
        "Responsible Sourcing and ESG Statement", "Orion Analytics Ltd",
        "<p>Our hardware is assembled by a single manufacturer in one region. We have not audited its labour "
        "practices.</p><p>Orion is carbon neutral; this claim has not been independently verified.</p>"),
}

SCANNED = ("08 Scanned/Board Minutes (scanned).pdf", page(
    "Minutes of a Meeting of the Board", "Orion Analytics Ltd, 2 June 2025",
    "<p>The board noted that FY2024 revenue was $21.8 million and that the Helios contract is up for renewal in "
    "2026, with Helios seeking a 15% price reduction.</p>"))

README = """FINE PRINT TEST PACK G: complete data room in PDF (practice company, not real)

Orion Analytics Ltd. 14 PDFs in 8 folders, one per due diligence workstream. Every problem is planted.
Open it as a deal (copy the folder into your deals folder, or run: python scripts/make_pdf_testpack.py --deals).

What to look for:
- Financial: revenue $24.6m in the CIM, $22.9m in the audited accounts (cross-document conflict, marked in both);
  the audited income statement does not add up (14,200 + 8,700 is 22,900, not 23,900); going concern and covenant
  breach; pro forma, run-rate and "management believes" claims.
- Earnings quality (the EBITDA check): claimed adjusted EBITDA $7.15m; the ledger shows legal fees and ERP costs every
  year, so those add-backs are disallowed and supported EBITDA is $5.6m; at 10x that is $15.5m of price. Severance is
  accepted as one-off.
- Customers: Helios Retail Group is 40% of ARR (deal breaker); churn 18%; pilots counted in ARR.
- Legal: change of control termination (MSA, supplier, lease); unlimited liability; exclusivity; most favoured nation;
  termination for convenience; auto-renewal; unsigned agreement with [TBD] dates; litigation and a patent claim while
  the CIM says there is no material litigation.
- Suppliers: single source with no exit plan; take-or-pay minimum commitment.
- People: key person (CTO) likely to leave, no non-compete; change of control bonuses; 28% attrition; contractors
  whose classification is being questioned; tribunal claim.
- Tax: open enquiry and transfer pricing, $1.4m at risk; unregistered VAT in Germany.
- Technology: 2023 data breach; end-of-life Windows Server 2012; no MFA; GPL component; untested backups.
- Property and ESG: lease expires March 2026 with no renewal; landlord consent on change of control; possible soil
  contamination; insurance certificate expired 31 March 2025; unverified carbon-neutral claim; unaudited supplier.
- Scanned: "Board Minutes (scanned).pdf" is an image only. Fine Print must say it cannot be checked, not pass it.
  (Its text would have shown a third revenue figure, $21.8m.)

Real PDF pages: in the Document view, PDFs are shown as their original pages with the marks drawn on them; switch to
"Text only" to see the extracted text.
"""


def browser() -> str:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    sys.exit("Edge or Chrome is needed to print the PDFs; none was found.")


def run(exe: str, *args: str) -> None:
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer", *args],
                   check=True, capture_output=True, timeout=120)


def print_pdf(exe: str, source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    run(exe, f"--print-to-pdf={target}", source.as_uri())


def print_scan(exe: str, source: Path, target: Path, work: Path) -> None:
    """A PDF that holds only a picture of the page, as a scanner would produce."""
    shot = work / "scan.png"
    run(exe, f"--screenshot={shot}", "--window-size=1240,1754", "--hide-scrollbars", source.as_uri())
    wrapper = work / "scan.html"
    wrapper.write_text("<!doctype html><html><head><style>@page{size:A4;margin:0}body{margin:0}"
                       "img{width:100%;display:block;filter:grayscale(1) contrast(1.1)}</style></head>"
                       f"<body><img src='{shot.as_uri()}'></body></html>", encoding="utf-8")
    print_pdf(exe, wrapper, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deals", action="store_true", help="also copy the pack into the deals folder")
    args = parser.parse_args()
    exe = browser()
    if OUT.exists():
        shutil.rmtree(OUT)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for i, (name, content) in enumerate(DOCS.items()):
            source = work / f"doc{i}.html"
            source.write_text(content, encoding="utf-8")
            print_pdf(exe, source, OUT / name)
            print(f"  {name}")
        source = work / "scanned.html"
        source.write_text(SCANNED[1], encoding="utf-8")
        print_scan(exe, source, OUT / SCANNED[0], work)
        print(f"  {SCANNED[0]} (image only)")
    (OUT / "_README.txt").write_text(README, encoding="utf-8")
    print(f"Wrote {len(DOCS) + 1} PDFs to {OUT}")
    if args.deals:
        deals = Path(os.getenv("DEALS_DIR") or Path.home() / "Deals")
        target = deals / OUT.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(OUT, target)
        print(f"Copied to {target}")


if __name__ == "__main__":
    main()
