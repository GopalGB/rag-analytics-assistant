"""Generate the fully synthetic demo dataset for the private-assistant prototype.

Everything here is FICTIONAL: the company ("Harbourline Property Group"), its suppliers, tenants,
contracts, invoices, bank lines and the QuickBooks sandbox fixture are invented for demonstration.
No real company, person, or financial data is used.

Outputs (committed to the repo, so running this script is optional):

    data/sample/documents/     contracts, lease, project record, policy   (.pdf / .docx / .md)
    data/sample/invoices/      supplier invoices, incl. one scanned (image-only) PDF
    data/sample/tables/        bank statement (.xlsx) + project budget (.csv)
    data/unseen_invoices/      hold-out invoices in NEW layouts — upload these live in the demo
    data/qbo_sandbox/          read-only QuickBooks Online test-company fixture (API JSON shape)
    data/ground_truth/         expected invoice fields, used by scripts/evaluate_extraction.py

Requires the dev extras (reportlab, python-docx, pillow): pip install -r requirements-dev.txt
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
UNSEEN = ROOT / "data" / "unseen_invoices"
QBO = ROOT / "data" / "qbo_sandbox"
TRUTH = ROOT / "data" / "ground_truth"

COMPANY = "Harbourline Property Group"
COMPANY_ADDR = ["Suite 400, 88 Harbour Street", "Port Easton, CA 90210"]
FICTIONAL = "Fictional demo document - synthetic data only"


# --------------------------------------------------------------------------- invoice specs
def money(x: float) -> str:
    return f"{x:,.2f}"


INVOICES = [
    {
        "file": "summit_INV-10421.pdf",
        "layout": "classic",
        "supplier": "Summit Ridge Electrical Supply LLC",
        "supplier_addr": ["410 Foundry Road", "Easton Heights, CA 90233"],
        "invoice_number": "INV-10421",
        "invoice_date": "2026-05-04",
        "date_text": "May 4, 2026",
        "due_date": "2026-06-03",
        "due_text": "June 3, 2026",
        "lines": [("Copper cable 2.5mm, 500m drum", 2, 1250.00), ("LED panel light 600x600", 40, 50.00)],
        "tax_rate": 0.0825,
        "po": "PO-2026-031",
    },
    {
        "file": "greenleaf_GL-2291.pdf",
        "layout": "modern",
        "supplier": "Greenleaf Grounds Care Co.",
        "supplier_addr": ["12 Willow Lane", "Port Easton, CA 90211"],
        "invoice_number": "GL-2291",
        "invoice_date": "2026-05-12",
        "date_text": "05/12/2026",  # deliberately ambiguous (May 12 or 5 Dec?)
        "due_date": "2026-06-11",
        "due_text": "06/11/2026",
        "lines": [("Monthly grounds maintenance - May", 1, 950.00), ("Hedge trimming (one-off)", 1, 300.00)],
        "tax_rate": 0.0,
    },
    {
        "file": "metro_MOS-7781.pdf",
        "layout": "tax",
        "supplier": "Metro Office Solutions Inc",
        "supplier_addr": ["900 Commerce Blvd", "Easton, CA 90201"],
        "invoice_number": "MOS-7781",
        "invoice_date": "2026-05-15",
        "date_text": "2026-05-15",
        "due_date": "2026-06-14",
        "due_text": "2026-06-14",
        "lines": [("A4 copy paper (box of 5 reams)", 12, 38.99), ("Toner cartridge TN-2450", 4, 42.25)],
        "tax_rate": 0.0825,
    },
    {
        "file": "coastal_5530.pdf",
        "layout": "classic",
        "supplier": "Coastal Plumbing & Heating Ltd",
        "supplier_addr": ["77 Pier Road", "Port Easton, CA 90212"],
        "invoice_number": "5530",
        "invoice_date": "2026-05-18",
        "date_text": "18 May 2026",
        "due_date": "2026-06-17",
        "due_text": "17 June 2026",
        "lines": [("Preventive maintenance - May (per agreement)", 1, 2310.00)],
        "tax_rate": 0.0,
    },
    {
        "file": "pioneer_PCW-0192_scanned.pdf",
        "layout": "scanned",
        "supplier": "Pioneer Concrete Works",
        "supplier_addr": ["5 Quarry Way", "Easton, CA 90240"],
        "invoice_number": "PCW-0192",
        "invoice_date": "2026-06-02",
        "date_text": "June 2, 2026",
        "due_date": "2026-07-02",
        "due_text": "July 2, 2026",
        "lines": [("Car park resurfacing - stage 1", 1, 12000.00)],
        "tax_rate": 0.0825,
    },
    {
        "file": "summit_INV-10421_resubmitted.pdf",
        "layout": "classic",
        "stamp": "COPY - RESUBMITTED",
        "supplier": "Summit Ridge Electrical Supply LLC",
        "supplier_addr": ["410 Foundry Road", "Easton Heights, CA 90233"],
        "invoice_number": "INV-10421",
        "invoice_date": "2026-05-04",
        "date_text": "May 4, 2026",
        "due_date": "2026-06-03",
        "due_text": "June 3, 2026",
        "lines": [("Copper cable 2.5mm, 500m drum", 2, 1250.00), ("LED panel light 600x600", 40, 50.00)],
        "tax_rate": 0.0825,
        "po": "PO-2026-031",
    },
    {
        "file": "harbor_waste_no_number.pdf",
        "layout": "modern",
        "supplier": "Harbor Waste Services",
        "supplier_addr": ["3 Depot Street", "Easton, CA 90205"],
        "invoice_number": None,  # missing on the document -> must be flagged, never invented
        "invoice_date": "2026-05-28",
        "date_text": "2026-05-28",
        "due_date": None,
        "due_text": None,
        "lines": [("Commercial bin collection - May", 1, 415.00)],
        "tax_rate": 0.0,
    },
    {
        "file": "brightspark_BSC-118.pdf",
        "layout": "tax",
        "supplier": "BrightSpark Cleaning Co",
        "supplier_addr": ["21 Mill Street", "Port Easton, CA 90214"],
        "invoice_number": "BSC-118",
        "invoice_date": "2026-06-05",
        "date_text": "2026-06-05",
        "due_date": "2026-06-19",
        "due_text": "2026-06-19",
        "lines": [("Common area cleaning - May", 1, 900.00)],
        "tax_rate": 0.0825,
        "total_override": 984.25,  # printed total does NOT equal subtotal + tax (974.25)
    },
]

UNSEEN_INVOICES = [
    {
        "file": "northgate_NSS-2026-044.pdf",
        "layout": "remittance",
        "supplier": "Northgate Security Systems",
        "supplier_addr": ["1400 Gate Avenue", "Easton, CA 90250"],
        "invoice_number": "NSS/2026/044",
        "invoice_date": "2026-06-21",
        "date_text": "21-06-2026",
        "due_date": "2026-07-21",
        "due_text": "21-07-2026",
        "lines": [("CCTV maintenance - Q2", 1, 1869.05), ("Access card readers", 4, 250.00)],
        "tax_rate": 0.0825,
    },
    {
        "file": "blueridge_BRIT-5521.pdf",
        "layout": "minimal",
        "supplier": "Blue Ridge IT Services",
        "supplier_addr": ["55 Summit Drive", "Easton, CA 90260"],
        "invoice_number": "BRIT-5521",
        "invoice_date": "2026-06-25",
        "date_text": "June 25, 2026",
        "due_date": "2026-07-25",
        "due_text": "July 25, 2026",
        "lines": [("Managed IT support - June", 1, 695.00), ("Laptop setup", 1, 150.00)],
        "tax_rate": 0.0,
    },
    {
        "file": "lakeside_catering_LC-3390.png",
        "layout": "photo",
        "supplier": "Lakeside Catering",
        "supplier_addr": ["8 Shore Road", "Port Easton, CA 90215"],
        "invoice_number": "LC-3390",
        "invoice_date": "2026-06-30",
        "date_text": "06/30/2026",
        "due_date": "2026-07-14",
        "due_text": "07/14/2026",
        "lines": [("Tenant open day catering", 1, 1097.09)],
        "tax_rate": 0.0825,
    },
]


def totals(spec: dict) -> tuple[float, float, float]:
    subtotal = round(sum(q * p for _, q, p in spec["lines"]), 2)
    tax = round(subtotal * spec["tax_rate"], 2)
    total = spec.get("total_override") or round(subtotal + tax, 2)
    return subtotal, tax, total


# --------------------------------------------------------------------------- invoice text body
def invoice_lines(spec: dict) -> list[str]:
    """Plain-text rendering of an invoice, varied by layout (used for scanned/photo images)."""
    sub, tax, total = totals(spec)
    num = spec["invoice_number"]
    out = [spec["supplier"].upper(), *spec["supplier_addr"], "", "INVOICE", ""]
    if num:
        out.append(f"Invoice No: {num}")
    out.append(f"Date: {spec['date_text']}")
    if spec.get("due_text"):
        out.append(f"Due Date: {spec['due_text']}")
    out += ["", "Bill To:", COMPANY, *COMPANY_ADDR, ""]
    out.append("Description                          Qty    Amount")
    for desc, q, p in spec["lines"]:
        out.append(f"{desc:<36} {q:>3}  {money(q * p):>10}")
    out += ["", f"Subtotal: ${money(sub)}", f"Sales Tax: ${money(tax)}", f"TOTAL: ${money(total)}"]
    return out


# --------------------------------------------------------------------------- PDF rendering
def draw_invoice_pdf(path: Path, spec: dict) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    sub, tax, total = totals(spec)
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    w, h = LETTER
    layout = spec["layout"]
    num = spec["invoice_number"]
    c.setTitle(f"Invoice {num or ''} - {spec['supplier']}")

    def lines_table(y: float, x_desc=60, x_qty=380, x_amt=520) -> float:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x_desc, y, "Description")
        c.drawRightString(x_qty, y, "Qty")
        c.drawRightString(x_qty + 70, y, "Unit price")
        c.drawRightString(x_amt, y, "Amount")
        c.line(55, y - 4, 530, y - 4)
        c.setFont("Helvetica", 10)
        for desc, q, p in spec["lines"]:
            y -= 18
            c.drawString(x_desc, y, desc)
            c.drawRightString(x_qty, y, str(q))
            c.drawRightString(x_qty + 70, y, money(p))
            c.drawRightString(x_amt, y, money(q * p))
        return y - 30

    if layout == "classic":
        c.setFont("Helvetica-Bold", 18)
        c.drawString(60, h - 70, spec["supplier"])
        c.setFont("Helvetica", 10)
        for i, line in enumerate(spec["supplier_addr"]):
            c.drawString(60, h - 88 - 13 * i, line)
        c.setFont("Helvetica-Bold", 22)
        c.drawRightString(530, h - 70, "INVOICE")
        c.setFont("Helvetica", 10)
        y = h - 150
        c.drawString(60, y, f"Invoice Number: {num}")
        c.drawString(60, y - 15, f"Invoice Date: {spec['date_text']}")
        c.drawString(60, y - 30, f"Due Date: {spec['due_text']}")
        if spec.get("po"):
            c.drawString(60, y - 45, f"PO Number: {spec['po']}")
        c.drawString(330, y, "Bill To:")
        c.drawString(330, y - 15, COMPANY)
        for i, line in enumerate(COMPANY_ADDR):
            c.drawString(330, y - 30 - 15 * i, line)
        y = lines_table(h - 250)
        c.drawRightString(450, y, "Subtotal")
        c.drawRightString(520, y, f"${money(sub)}")
        c.drawRightString(450, y - 16, f"Sales Tax ({spec['tax_rate'] * 100:.2f}%)")
        c.drawRightString(520, y - 16, f"${money(tax)}")
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(450, y - 36, "Total Due")
        c.drawRightString(520, y - 36, f"${money(total)}")
        c.setFont("Helvetica", 9)
        c.drawString(60, 90, "Payment terms: Net 30. Please quote the invoice number with your remittance.")
    elif layout == "modern":
        c.setFillColorRGB(0.12, 0.45, 0.30)
        c.rect(0, h - 110, w, 110, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(50, h - 60, spec["supplier"])
        c.setFont("Helvetica", 10)
        c.drawString(50, h - 80, " | ".join(spec["supplier_addr"]))
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, h - 145, "Invoice")
        c.setFont("Helvetica", 10)
        y = h - 165
        if num:
            c.drawString(50, y, f"Invoice #: {num}")
            y -= 15
        c.drawString(50, y, f"Date: {spec['date_text']}")
        if spec.get("due_text"):
            c.drawString(50, y - 15, f"Payment due: {spec['due_text']}")
        c.drawString(330, h - 165, f"Customer: {COMPANY}")
        y = lines_table(h - 240)
        c.setFont("Helvetica-Bold", 13)
        c.drawRightString(520, y, f"Amount Due: ${money(total)}")
        c.setFont("Helvetica", 9)
        c.drawString(50, 90, "Thank you for your business.")
    elif layout == "tax":
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(w / 2, h - 60, "TAX INVOICE")
        c.setFont("Helvetica-Bold", 12)
        c.drawString(60, h - 95, spec["supplier"])
        c.setFont("Helvetica", 10)
        c.drawString(60, h - 110, ", ".join(spec["supplier_addr"]))
        y = h - 150
        c.drawString(60, y, f"Inv No.: {num}")
        c.drawString(220, y, f"Issued: {spec['date_text']}")
        c.drawString(380, y, f"Due: {spec['due_text']}")
        c.drawString(60, y - 20, f"Invoice to: {COMPANY}, {COMPANY_ADDR[0]}")
        y = lines_table(h - 220)
        c.drawRightString(450, y, "Net amount")
        c.drawRightString(520, y, money(sub))
        c.drawRightString(450, y - 16, "Tax")
        c.drawRightString(520, y - 16, money(tax))
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(450, y - 36, "BALANCE DUE USD")
        c.drawRightString(520, y - 36, money(total))
    elif layout == "remittance":
        c.setFont("Helvetica", 9)
        c.drawString(60, h - 50, "Tax Invoice / Remittance Advice")
        c.setFont("Helvetica-Bold", 15)
        c.drawString(60, h - 75, spec["supplier"])
        c.setFont("Helvetica", 10)
        c.drawString(60, h - 92, " / ".join(spec["supplier_addr"]))
        y = h - 135
        c.drawString(60, y, f"Invoice Ref: {num}")
        c.drawString(60, y - 15, f"Invoice date: {spec['date_text']}")
        c.drawString(60, y - 30, f"Payment by: {spec['due_text']}")
        c.drawString(330, y, "Charged to:")
        c.drawString(330, y - 15, COMPANY)
        y = lines_table(h - 215)
        c.drawRightString(450, y, "Goods & services")
        c.drawRightString(520, y, money(sub))
        c.drawRightString(450, y - 16, "Sales tax 8.25%")
        c.drawRightString(520, y - 16, money(tax))
        c.setFont("Helvetica-Bold", 12)
        c.drawString(60, y - 50, f"Total amount payable: USD {money(total)}")
    elif layout == "minimal":
        c.setFont("Helvetica-Bold", 13)
        c.drawString(60, h - 60, spec["supplier"])
        c.setFont("Helvetica", 10)
        y = h - 100
        c.drawString(60, y, f"Invoice ID: {num}")
        c.drawString(60, y - 15, f"Date of issue: {spec['date_text']}")
        c.drawString(60, y - 30, f"Pay by: {spec['due_text']}")
        c.drawString(60, y - 45, f"Client: {COMPANY}")
        y = lines_table(h - 190)
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(520, y, f"Grand Total: ${money(total)}")
    if spec.get("stamp"):
        c.saveState()
        c.setFillColorRGB(0.8, 0.1, 0.1)
        c.setFont("Helvetica-Bold", 28)
        c.translate(300, 380)
        c.rotate(25)
        c.drawCentredString(0, 0, spec["stamp"])
        c.restoreState()
    c.setFont("Helvetica-Oblique", 7)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(60, 40, FICTIONAL)
    c.showPage()
    c.save()


def render_invoice_image(spec: dict, rotate: float = 0.6, seed: int = 3):
    """Render an invoice as a grayscale 'scan'/'photo' image (no text layer) for the OCR path."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = random.Random(seed)
    img = Image.new("L", (1275, 1650), 250)  # letter @150dpi
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=26)
    bold = ImageFont.load_default(size=34)
    y = 90
    for i, line in enumerate(invoice_lines(spec)):
        f = bold if i in (0, 4) else font
        draw.text((110, y), line, fill=25, font=f)
        y += 44 if f is bold else 38
    draw.text((110, 1560), FICTIONAL, fill=90, font=ImageFont.load_default(size=16))
    # scanner artefacts: speckle noise, slight blur and skew
    px = img.load()
    for _ in range(9000):
        x, yy = rng.randrange(img.width), rng.randrange(img.height)
        px[x, yy] = rng.choice((150, 190, 215))
    img = img.filter(ImageFilter.GaussianBlur(0.6)).rotate(rotate, fillcolor=245, expand=False)
    return img


# --------------------------------------------------------------------------- documents
def pdf_document(path: Path, title: str, pages: list[list[str]]) -> None:
    """Write a simple multi-page text PDF: each page is a list of paragraphs/headings (## prefix)."""
    import textwrap

    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    c.setTitle(title)
    w, h = LETTER
    for pno, paragraphs in enumerate(pages, start=1):
        y = h - 60
        if pno == 1:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(60, y, title)
            y -= 30
        for para in paragraphs:
            if para.startswith("## "):
                y -= 6
                c.setFont("Helvetica-Bold", 12)
                c.drawString(60, y, para[3:])
                y -= 18
                continue
            c.setFont("Helvetica", 10)
            for line in textwrap.wrap(para, 95) or [""]:
                c.drawString(60, y, line)
                y -= 14
            y -= 6
        c.setFont("Helvetica-Oblique", 7)
        c.drawString(60, 40, f"{FICTIONAL} - page {pno} of {len(pages)}")
        c.showPage()
    c.save()


SUPPLIER_AGREEMENT = [
    [
        "This Supply Agreement is made between Harbourline Property Group (the \"Customer\") and "
        "Summit Ridge Electrical Supply LLC (the \"Supplier\"). Effective date: 1 January 2026.",
        "## 1. Term",
        "The agreement runs for an initial term of 24 months from the effective date and renews "
        "automatically for successive 12-month periods unless either party gives notice.",
        "## 2. Pricing",
        "Copper cable 2.5mm, 500m drum: $1,250.00 per drum. LED panel light 600x600: $50.00 each. "
        "Emergency call-out labour: $95.00 per hour, minimum two hours. Prices exclude sales tax.",
        "Prices may be reviewed once per year on 1 January, limited to the change in CPI and capped "
        "at 4% per annum. Any price change requires 30 days written notice.",
        "## 3. Payment terms",
        "Invoices are payable within 30 days of the invoice date (Net 30). Every invoice must quote "
        "a valid Customer purchase order (PO) number. Invoices without a PO may be returned unpaid.",
    ],
    [
        "## 4. Delivery and acceptance",
        "Goods are delivered to the site nominated in the purchase order. The Customer may reject "
        "defective goods within 14 days of delivery.",
        "## 5. Termination",
        "Either party may terminate this agreement for convenience by giving 60 days written notice. "
        "Either party may terminate immediately for material breach not remedied within 15 days.",
        "## 6. Liability and insurance",
        "Each party's total liability is capped at the fees paid under this agreement in the 12 "
        "months before the claim. The Supplier must hold public liability insurance of at least "
        "$2,000,000 and provide a certificate on request.",
        "## 7. Contacts",
        "Supplier account manager: Dana Whitfield (fictional). Customer contact: Facilities Manager.",
    ],
]

MAINTENANCE_AGREEMENT = [
    [
        "Maintenance Services Agreement between Harbourline Property Group and Coastal Plumbing & "
        "Heating Ltd. Effective date: 1 March 2026. Term: 12 months.",
        "## Scope",
        "Monthly preventive maintenance of plumbing, hot-water and heating systems at all managed "
        "properties, plus reactive repairs on request.",
        "## Fees",
        "Preventive maintenance fee: $2,310.00 per month, invoiced monthly in arrears, no sales tax "
        "applies to labour. Reactive repairs are charged at $110.00 per hour plus materials at cost.",
        "## Payment",
        "Invoices are payable within 30 days. Disputed amounts must be raised within 10 business days.",
        "## Service levels",
        "Emergency response within 4 hours, 24/7. Non-urgent requests within 2 business days.",
    ]
]

PROJECT_STATUS = [
    [
        "Project: Riverside Apartments - Common Area Renovation. Report date: 15 June 2026. "
        "Project manager: Facilities team.",
        "## Budget",
        "Approved budget: $185,000. Committed to date: $142,300. Spent to date: $118,650. "
        "Forecast at completion: $191,400, which is $6,400 (3.5%) over budget, mainly due to the "
        "car park resurfacing variation from Pioneer Concrete Works.",
        "## Schedule",
        "Target practical completion: 29 August 2026. Current status: amber, about 2 weeks behind "
        "because of the delayed electrical sign-off.",
        "## Outstanding tasks",
        "1. Electrical compliance sign-off for lobby lighting (Summit Ridge Electrical) - due 30 June 2026.",
        "2. Landscaping phase 2 quote from Greenleaf Grounds Care - requested, not yet received.",
        "3. Fire door inspection certificate - OVERDUE since 1 June 2026.",
        "4. Lift modernisation approval - waiting on board decision at the July meeting.",
        "5. Final defects walk-through - not yet scheduled.",
    ],
    [
        "## Risks",
        "Budget overrun if the lift modernisation is approved in this financial year. Missing fire "
        "door certificate could delay the occupancy sign-off.",
        "## Decisions needed",
        "Approve the $6,400 budget variation. Confirm whether the lift modernisation is in scope.",
    ],
]

POLICY_MD = """# Expense and Invoice Approval Policy

Fictional demo document - synthetic data only.

## Approval thresholds
- Invoices under $1,000: approved by the budget-holding manager.
- Invoices from $1,000 up to $5,000: approved by the finance lead.
- Invoices over $5,000: approved by a director, and two quotes must be on file.

## Invoice checks before payment
1. The invoice must match an approved purchase order (PO) where one is required by contract.
2. Check for duplicates: same supplier and invoice number must never be paid twice.
3. Subtotal plus tax must equal the invoice total.
4. Any change to supplier bank details must be verified by phone using a known number.

## Recording
All approved supplier invoices must be recorded as bills in the accounting system within 5
business days of receipt. Nothing may be paid or recorded without the required approval.
"""

LEASE_PARAGRAPHS = [
    ("h", "Office Lease Summary - Suite 400, 88 Harbour Street"),
    ("p", FICTIONAL),
    ("p", "Landlord: Pier Nine Holdings (fictional). Tenant: Harbourline Property Group."),
    ("h2", "Key dates"),
    ("p", "Commencement date: 1 July 2024. Expiry date: 30 June 2029. Term: 5 years."),
    ("p", "Renewal option: one further term of 5 years. The tenant must give written notice of "
          "renewal at least 6 months before expiry, i.e. no later than 31 December 2028."),
    ("h2", "Rent"),
    ("p", "Base rent: $8,500.00 per month, payable monthly in advance on the 1st."),
    ("p", "Rent review: fixed increase of 3% on each 1 July anniversary."),
    ("p", "Security deposit: $25,500.00 (three months' rent), held by the landlord."),
    ("h2", "Responsibilities"),
    ("p", "The landlord maintains the structure, roof and building services. The tenant is "
          "responsible for internal repairs, cleaning and its own utilities."),
    ("p", "Make-good: the tenant must return the premises in original condition, fair wear and tear excepted."),
]


def write_docx(path: Path) -> None:
    import docx

    d = docx.Document()
    for kind, text in LEASE_PARAGRAPHS:
        if kind == "h":
            d.add_heading(text, level=1)
        elif kind == "h2":
            d.add_heading(text, level=2)
        else:
            d.add_paragraph(text)
    d.core_properties.author = "Demo"
    d.save(str(path))


# --------------------------------------------------------------------------- tables
def write_tables(out: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["date", "description", "reference", "debit", "credit", "balance"])
    balance = 52_340.00
    rows = [
        ("2026-05-01", "Opening balance", "", None, None),
        ("2026-05-02", "Rent received - Cobalt Dental Clinic", "INV 1001", None, 8500.00),
        ("2026-05-05", "Rent received - Juniper Yoga Studio", "INV 1003 part", None, 1700.00),
        ("2026-05-08", "Payroll", "PAY-0508", 14200.00, None),
        ("2026-05-15", "Utilities - Easton Power & Water", "DD-4471", 1382.40, None),
        ("2026-05-22", "Allied Fuel Cards", "AFC-3302", 940.00, None),
        ("2026-06-01", "Summit Ridge Electrical Supply", "INV-10421", 4871.25, None),
        ("2026-06-02", "Rent received - Cobalt Dental Clinic", "INV 1004", None, 8500.00),
        ("2026-06-08", "Payroll", "PAY-0608", 14200.00, None),
        ("2026-06-12", "Bank fee", "FEE", 25.00, None),
    ]
    for d, desc, ref, debit, credit in rows:
        balance = balance - (debit or 0) + (credit or 0)
        ws.append([d, desc, ref, debit, credit, round(balance, 2)])
    wb.save(str(out / "bank_statement_2026_q2.xlsx"))

    with (out / "project_budget_riverside.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["line_item", "supplier", "budget", "committed", "spent"])
        w.writerow(["Lobby lighting upgrade", "Summit Ridge Electrical Supply LLC", 38000, 36500, 31200])
        w.writerow(["Landscaping phase 1", "Greenleaf Grounds Care Co.", 22000, 21500, 21500])
        w.writerow(["Landscaping phase 2", "Greenleaf Grounds Care Co.", 18000, 0, 0])
        w.writerow(["Car park resurfacing", "Pioneer Concrete Works", 55000, 61400, 12990])
        w.writerow(["Painting and finishes", "Internal", 32000, 22900, 22900])
        w.writerow(["Contingency", "", 20000, 0, 0])


# --------------------------------------------------------------------------- QuickBooks fixture
def qbo_fixture() -> dict:
    vendors = [
        "Summit Ridge Electrical Supply LLC", "Greenleaf Grounds Care Co.", "Coastal Plumbing & Heating Ltd",
        "Pioneer Concrete Works", "Allied Fuel Cards", "Harbor Waste Services", "Northgate Security Systems",
        "Lakeside Catering", "Metro Office Solutions Inc",
    ]
    vendor_ids = {name: str(50 + i) for i, name in enumerate(vendors)}

    def bill(bid, vendor, doc, date, due, amt, bal):
        b = {
            "Id": bid, "SyncToken": "0", "TxnDate": date, "DueDate": due, "TotalAmt": amt, "Balance": bal,
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
            "VendorRef": {"value": vendor_ids[vendor], "name": vendor},
            "APAccountRef": {"value": "33", "name": "Accounts Payable (A/P)"},
        }
        if doc:
            b["DocNumber"] = doc
        return b

    bills = [
        bill("101", "Summit Ridge Electrical Supply LLC", "INV-10421", "2026-05-04", "2026-06-03", 4871.25, 0),
        bill("102", "Greenleaf Grounds Care Co.", "GL-2291", "2026-05-12", "2026-06-11", 1250.00, 1250.00),
        bill("103", "Coastal Plumbing & Heating Ltd", "5530", "2026-05-18", "2026-06-17", 2130.00, 2130.00),
        bill("104", "Pioneer Concrete Works", "PCW-0192", "2026-06-02", "2026-07-02", 12990.00, 12990.00),
        bill("105", "Allied Fuel Cards", "AFC-3302", "2026-05-20", "2026-06-19", 940.00, 0),
        bill("106", "Harbor Waste Services", None, "2026-05-28", "2026-06-27", 415.00, 415.00),
        bill("107", "Northgate Security Systems", "NSS/2026/044", "2026-06-21", "2026-07-21", 3105.75, 3105.75),
        bill("108", "Lakeside Catering", "LC-3390", "2026-06-30", "2026-07-14", 1187.60, 1187.60),
    ]
    customers = ["Cobalt Dental Clinic", "Oakridge Accounting Partners", "Juniper Yoga Studio"]
    cust_ids = {name: str(10 + i) for i, name in enumerate(customers)}

    def inv(iid, doc, cust, date, due, amt, bal):
        return {
            "Id": iid, "DocNumber": doc, "TxnDate": date, "DueDate": due, "TotalAmt": amt, "Balance": bal,
            "CustomerRef": {"value": cust_ids[cust], "name": cust},
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
        }

    invoices = [
        inv("201", "1001", "Cobalt Dental Clinic", "2026-05-01", "2026-05-01", 8500.00, 0),
        inv("202", "1002", "Oakridge Accounting Partners", "2026-05-01", "2026-06-01", 6200.00, 6200.00),
        inv("203", "1003", "Juniper Yoga Studio", "2026-05-01", "2026-05-31", 3400.00, 1700.00),
        inv("204", "1004", "Cobalt Dental Clinic", "2026-06-01", "2026-06-01", 8500.00, 0),
        inv("205", "1005", "Oakridge Accounting Partners", "2026-06-01", "2026-07-01", 6200.00, 6200.00),
    ]
    accounts = [
        ("35", "Business Checking", "Bank", "Checking", 35166.35),
        ("33", "Accounts Payable (A/P)", "Accounts Payable", "AccountsPayable", 21077.60),
        ("84", "Accounts Receivable (A/R)", "Accounts Receivable", "AccountsReceivable", 14100.00),
        ("60", "Repairs and Maintenance", "Expense", "RepairMaintenance", 24906.00),
        ("61", "Utilities", "Expense", "Utilities", 1382.40),
        ("62", "Office Supplies", "Expense", "OfficeGeneralAdministrativeExpenses", 0.0),
        ("79", "Rental Income", "Income", "OtherPrimaryIncome", 32800.00),
    ]
    return {
        "CompanyInfo": [
            {
                "Id": "1", "CompanyName": "Sandbox Company (fictional test data)",
                "Country": "US", "FiscalYearStartMonth": "January",
            }
        ],
        "Vendor": [
            {"Id": vendor_ids[n], "DisplayName": n, "Active": True, "Balance": 0.0} for n in vendors
        ],
        "Customer": [
            {"Id": cust_ids[n], "DisplayName": n, "Active": True, "Balance": 0.0} for n in customers
        ],
        "Bill": bills,
        "Invoice": invoices,
        "Account": [
            {"Id": i, "Name": n, "AccountType": t, "AccountSubType": st, "CurrentBalance": bal, "Active": True}
            for i, n, t, st, bal in accounts
        ],
    }


# --------------------------------------------------------------------------- main
def truth(spec: dict, split: str) -> dict:
    sub, tax, total = totals(spec)
    if spec["layout"] in ("modern", "minimal"):  # these layouts print only the amount due
        sub = tax = None
    return {
        "file": spec["file"],
        "split": split,
        "supplier": spec["supplier"],
        "invoice_number": spec["invoice_number"],
        "invoice_date": spec["invoice_date"],
        "due_date": spec["due_date"],
        "subtotal": sub,
        "tax": tax,
        "total": total,
    }


def write_invoice(spec: dict, folder: Path) -> None:
    path = folder / spec["file"]
    if spec["layout"] == "scanned":
        render_invoice_image(spec, rotate=0.7).convert("RGB").save(str(path), "PDF", resolution=150.0)
    elif spec["layout"] == "photo":
        render_invoice_image(spec, rotate=-0.9, seed=11).save(str(path))
    else:
        draw_invoice_pdf(path, spec)


def main() -> None:
    for d in (SAMPLE / "documents", SAMPLE / "invoices", SAMPLE / "tables", UNSEEN, QBO, TRUTH):
        d.mkdir(parents=True, exist_ok=True)

    for spec in INVOICES:
        write_invoice(spec, SAMPLE / "invoices")
    for spec in UNSEEN_INVOICES:
        write_invoice(spec, UNSEEN)

    docs = SAMPLE / "documents"
    pdf_document(docs / "Supplier_Agreement_Summit_Ridge_Electrical.pdf", "Supply Agreement", SUPPLIER_AGREEMENT)
    pdf_document(
        docs / "Maintenance_Agreement_Coastal_Plumbing.pdf", "Maintenance Services Agreement", MAINTENANCE_AGREEMENT
    )
    pdf_document(docs / "Project_Status_Riverside_Renovation.pdf", "Project Status Report", PROJECT_STATUS)
    (docs / "Expense_and_Invoice_Approval_Policy.md").write_text(POLICY_MD, encoding="utf-8")
    write_docx(docs / "Office_Lease_Summary.docx")

    write_tables(SAMPLE / "tables")
    (QBO / "sandbox_company.json").write_text(json.dumps(qbo_fixture(), indent=2), encoding="utf-8")

    labels = [truth(s, "sample") for s in INVOICES] + [truth(s, "unseen") for s in UNSEEN_INVOICES]
    (TRUTH / "invoices.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    print(f"Wrote synthetic demo data under {ROOT / 'data'}")


if __name__ == "__main__":
    main()
