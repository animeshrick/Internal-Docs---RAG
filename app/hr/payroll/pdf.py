"""
hr/payroll/pdf.py - renders one employee's payslip for one month as a PDF.

Pure rendering: it takes plain dictionaries and numbers in, and returns PDF bytes out.
Nothing here reads the database or talks to storage; service.py does that and calls this.

Layout: a narrow left column of statutory details (employee code, PAN, bank account, IFSC,
date of joining, payable days, tax regime) next to a wide right column with the net-pay summary
strip and the Gross Pay (A) / Deductions (B) breakdown tables - the format the company settled
on for Semicolon ITES payslips (see areas/hrms-payslips-and-notifications.md in the project).
"""
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COMPANY_NAME = "Semicolon ITES"
COMPANY_ADDRESS = "Durgapur, West Bengal, India"

# Colours lifted from the HRMS frontend's design tokens (HRMS-Frontend/src/index.css), so the
# payslip looks like it belongs to the same product as the website and the chat.
INK = colors.HexColor("#1f1f22")
INK_2 = colors.HexColor("#4a4a50")
INK_3 = colors.HexColor("#696970")
BRAND = colors.HexColor("#f8d058")
BRAND_INK = colors.HexColor("#26262a")
LINE = colors.HexColor("#e1e1dc")
SURFACE_2 = colors.HexColor("#efefec")
GROSS_ACCENT = colors.HexColor("#2f9e44")       # green - Gross Pay (A)
DEDUCTION_ACCENT = colors.HexColor("#e8590c")   # orange - Deductions (B)

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(n):
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")


def _three_digits(n):
    if n >= 100:
        return f"{_ONES[n // 100]} Hundred" + (f" {_two_digits(n % 100)}" if n % 100 else "")
    return _two_digits(n)


def amount_in_words(amount):
    """4821 -> 'Four Thousand Eight Hundred Twenty One'. Good enough for typical salary figures."""
    n = int(round(amount))
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(f"{_three_digits(crore)} Crore")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(f"{_three_digits(lakh)} Lakh")
    thousand, n = divmod(n, 1000)
    if thousand:
        parts.append(f"{_three_digits(thousand)} Thousand")
    if n:
        parts.append(_three_digits(n))
    return " ".join(parts)


def money(amount):
    amount = float(amount)
    return f"Rs. {amount:,.0f}" if amount == int(amount) else f"Rs. {amount:,.2f}"


def _format_doj(doj_text):
    """'2026-08-10' -> '10 Aug 2026'. Anything unparsable is shown as given, so the page never breaks over it."""
    if not doj_text:
        return None
    try:
        return date.fromisoformat(str(doj_text)).strftime("%d %b %Y")
    except ValueError:
        return str(doj_text)


def _tax_regime_label(tax_regime):
    return {"new": "New Regime", "old": "Old Regime"}.get((tax_regime or "").lower(), "Not recorded")


def _field_block(label, value, label_style, value_style):
    """One stacked "label above value" entry for the left info column, as its own small flowable list."""
    return [Paragraph(label, label_style), Paragraph(value if value not in (None, "") else "Not recorded", value_style), Spacer(1, 9)]


def _info_column(employee, payable_days, label_style, value_style):
    rows = [
        ("Employee Code", employee.get("emp_id")),
        ("Name", employee.get("full_name")),
        ("PAN", employee.get("pan")),
        ("Account no.", employee.get("bank_account_number")),
        ("IFSC code", employee.get("ifsc_code")),
        ("Date of joining", _format_doj(employee.get("doj"))),
        ("Payable Days", str(payable_days) if payable_days is not None else None),
        ("Regime Opted", _tax_regime_label(employee.get("tax_regime"))),
    ]
    content = []
    for label, value in rows:
        content.extend(_field_block(label, value, label_style, value_style))
    return content


def _summary_strip(net_pay, total_earnings, total_deductions):
    net_label_style = ParagraphStyle("NetLabel", fontName="Helvetica", fontSize=8.5, textColor=INK_3, leading=11)
    net_amount_style = ParagraphStyle("NetAmount", fontName="Helvetica-Bold", fontSize=15, textColor=BRAND_INK, leading=18)
    eq_style = ParagraphStyle("Eq", fontName="Helvetica", fontSize=15, textColor=INK_3, alignment=TA_CENTER, leading=18)
    part_label_style = ParagraphStyle("PartLabel", fontName="Helvetica", fontSize=8.5, textColor=INK_3, leading=11)
    part_amount_style = ParagraphStyle("PartAmount", fontName="Helvetica-Bold", fontSize=12, textColor=INK, leading=15)

    net_cell = [Paragraph("Net Pay", net_label_style), Paragraph(money(net_pay), net_amount_style)]
    gross_cell = [Paragraph("Gross Pay (A)", part_label_style), Paragraph(f"+ {money(total_earnings)}", part_amount_style)]
    deduction_cell = [Paragraph("Deductions (B)", part_label_style), Paragraph(f"- {money(total_deductions)}", part_amount_style)]

    table = Table([[net_cell, Paragraph("=", eq_style), gross_cell, deduction_cell]], colWidths=[46 * mm, 10 * mm, 44 * mm, 44 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBEFORE", (2, 0), (2, 0), 2.2, GROSS_ACCENT),
        ("LINEBEFORE", (3, 0), (3, 0), 2.2, DEDUCTION_ACCENT),
        ("LEFTPADDING", (2, 0), (3, 0), 8),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _breakdown_table(rows, total_label, total_amount, header_text, label_style, value_style):
    """Earnings or deductions, each row shown as "Monthly" and "Total Amount" (the same figure
    for now - payslips are not yet prorated for partial months), ending with the bold total."""
    data = [[Paragraph(f"<b>{header_text}</b>", label_style), Paragraph("<b>Monthly</b>", label_style), Paragraph("<b>Total Amount</b>", label_style)]]
    for name, amount in rows:
        data.append([Paragraph(name, value_style), Paragraph(money(amount), value_style), Paragraph(money(amount), value_style)])
    if not rows:
        data.append([Paragraph("Not recorded", value_style), Paragraph(money(0), value_style), Paragraph(money(0), value_style)])
    data.append([Paragraph(f"<b>{total_label}</b>", value_style), Paragraph("", value_style), Paragraph(f"<b>{money(total_amount)}</b>", value_style)])
    table = Table(data, colWidths=[62 * mm, 30 * mm, 30 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SURFACE_2),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK_3),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
    ]))
    return table


def _draw_watermark(c, doc):
    c.saveState()
    c.setFillColor(colors.HexColor("#f5f5f3"))
    c.setFont("Helvetica-Bold", 60)
    c.translate(A4[0] / 2, A4[1] / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, COMPANY_NAME.upper())
    c.restoreState()
    c.saveState()
    c.setFont("Helvetica", 7.5)
    c.setFillColor(INK_3)
    c.drawRightString(A4[0] - 22 * mm, 10 * mm, f"Page {doc.page}")
    c.restoreState()


def build_payslip_pdf(employee, components, month_label, net_pay, payable_days=None):
    """
    employee: {"full_name", "emp_id", "dept", "role", "doj", "pan", "bank_account_number",
               "ifsc_code", "tax_regime"} (any value may be None/missing - every field falls
               back to "Not recorded" rather than breaking the page).
    components: rows from payroll_store.list_salary_components (component_name, component_type, monthly_amount).
    month_label: "November 2026" (use payroll_service.format_month).
    net_pay: the number actually paid (may differ slightly from earnings - deductions, which is fine).
    payable_days: calendar days in the pay month (use payroll_service.payable_days_in_month).
    Returns the finished PDF as bytes.
    """
    earnings = [(c["component_name"], c["monthly_amount"]) for c in components if c["component_type"] == "earning"]
    deductions = [(c["component_name"], c["monthly_amount"]) for c in components if c["component_type"] == "deduction"]
    total_earnings = sum(amount for _, amount in earnings) or net_pay
    total_deductions = sum(amount for _, amount in deductions)

    name_style = ParagraphStyle("CompanyName", fontName="Helvetica-Bold", fontSize=15, textColor=BRAND_INK, leading=18)
    addr_style = ParagraphStyle("CompanyAddr", fontName="Helvetica", fontSize=8.5, textColor=INK_3, leading=11)
    title_style = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=13, textColor=INK, alignment=TA_RIGHT, leading=16)
    sub_style = ParagraphStyle("Sub", fontName="Helvetica", fontSize=9, textColor=INK_3, alignment=TA_RIGHT, leading=12)
    info_label_style = ParagraphStyle("InfoLabel", fontName="Helvetica", fontSize=8, textColor=INK_3, leading=10)
    info_value_style = ParagraphStyle("InfoValue", fontName="Helvetica-Bold", fontSize=9.5, textColor=INK, leading=12)
    section_style = ParagraphStyle("Section", fontName="Helvetica-Bold", fontSize=10, textColor=INK, leading=13)
    section_sub_style = ParagraphStyle("SectionSub", fontName="Helvetica", fontSize=7.5, textColor=INK_3, leading=10)
    footer_style = ParagraphStyle("Footer", fontName="Helvetica", fontSize=7.5, textColor=INK_3, alignment=TA_CENTER, leading=10)
    net_words_style = ParagraphStyle("NetWords", fontName="Helvetica-Oblique", fontSize=8.5, textColor=INK_2, leading=11)

    story = []

    # ---- header: company name on the left, "Payslip: <month>" on the right
    logo_cell = Table([[""]], colWidths=[9 * mm], rowHeights=[9 * mm])
    logo_cell.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BRAND)]))
    company_block = Table([[logo_cell, Paragraph(f"<b>{COMPANY_NAME}</b>", name_style)]], colWidths=[11 * mm, 70 * mm])
    company_block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    header_table = Table(
        [[
            [company_block, Spacer(1, 2), Paragraph(COMPANY_ADDRESS, addr_style)],
            [Paragraph(f"Payslip: {month_label}", title_style), Spacer(1, 2), Paragraph("Generated by the HR portal", sub_style)],
        ]],
        colWidths=[90 * mm, None],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND, spaceAfter=10))

    # ---- net pay summary strip: Net Pay = Gross Pay (A) + ... | Deductions (B) - ...
    story.append(_summary_strip(net_pay, total_earnings, total_deductions))
    story.append(Spacer(1, 12))

    # ---- left info column + right breakdown column, side by side
    info_column = _info_column(employee, payable_days, info_label_style, info_value_style)

    right_column = [
        Paragraph("Gross Pay (A)", section_style),
        Paragraph("The total money you earned before the deductions", section_sub_style),
        Spacer(1, 4),
        _breakdown_table(earnings, "Gross Pay", total_earnings, "Earnings", info_label_style, info_value_style),
        Spacer(1, 12),
        Paragraph("Deductions (B)", section_style),
        Paragraph("The amount deducted for taxes and other benefits", section_sub_style),
        Spacer(1, 4),
        _breakdown_table(deductions, "Total Deductions", total_deductions, "Deductions", info_label_style, info_value_style),
    ]

    body = Table([[info_column, right_column]], colWidths=[42 * mm, 128 * mm])
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 10),
        ("LEFTPADDING", (1, 0), (1, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("LINEAFTER", (0, 0), (0, 0), 0.6, LINE),
    ]))
    story.append(body)
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Net pay in words: Rupees {amount_in_words(net_pay)} Only.", net_words_style))
    story.append(Spacer(1, 20))

    story.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceAfter=10))
    story.append(Paragraph(
        "This is a computer-generated payslip and does not require a signature. For any questions about this "
        "payslip, please contact HR.", footer_style,
    ))
    story.append(Paragraph(
        f"{COMPANY_NAME} &middot; Generated via the HR portal &middot; Confidential &mdash; for "
        f"{employee.get('full_name') or 'the employee'} only.", footer_style,
    ))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    doc.build(story, onFirstPage=_draw_watermark, onLaterPages=_draw_watermark)
    return buffer.getvalue()
