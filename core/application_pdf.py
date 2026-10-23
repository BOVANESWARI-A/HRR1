from io import BytesIO
from pathlib import Path

import base64
from xml.sax.saxutils import escape as xml_escape
from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

TEAL = colors.HexColor("#0b7a75")
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#5b6770")
LINE = colors.HexColor("#d9e0e3")
HEADER_BG = colors.HexColor("#eef6f5")
CARD_BG = colors.HexColor("#f8fafb")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
PAGE_WIDTH = 180 * mm
BODY_PAD = 8 * mm
INNER_WIDTH = PAGE_WIDTH - (2 * BODY_PAD)
LOGO_COL = 32 * mm
CONTENT_COL = INNER_WIDTH - LOGO_COL
IMAGES_DIR = Path(__file__).resolve().parent / "static" / "core" / "assets" / "images"


def _text(value, fallback="—"):
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _escape(value):
    return (
        _text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


FILE_LABELS = {
    "personality_test_result": "Personality Test Result",
    "updated_resume": "Updated Resume",
    "identity_document": "MyKad/NRIC/Passport",
    "academic_certificate": "Academic Certificate",
    "payslips": "Pay Slip",
    "payslip": "Pay Slip",
    "payslip_1": "Pay Slip 1",
    "payslip_2": "Pay Slip 2",
    "payslip_3": "Pay Slip 3",
    "applicant_signature": "Applicant's Signatory",
    "macro_doc_1_1": "Passport Size Photo",
    "macro_doc_5_1": "Candidate's Signature",
}


def _pretty_field(name):
    key = (name or "").strip()
    if key in FILE_LABELS:
        return FILE_LABELS[key]
    return _text(key, "Document").replace("_", " ").title()


def _styles():
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "PdfBrand",
            parent=base["Title"],
            fontSize=16,
            textColor=TEAL,
            spaceAfter=2,
            alignment=TA_LEFT,
            leading=20,
        ),
        "subtitle": ParagraphStyle(
            "PdfSubtitle",
            parent=base["Normal"],
            fontSize=11,
            textColor=INK,
            spaceAfter=2,
            leading=14,
        ),
        "meta": ParagraphStyle(
            "PdfMeta",
            parent=base["Normal"],
            fontSize=9,
            textColor=MUTED,
            spaceAfter=8,
            leading=12,
        ),
        "section": ParagraphStyle(
            "PdfSection",
            parent=base["Heading2"],
            fontSize=11,
            textColor=TEAL,
            spaceBefore=0,
            spaceAfter=0,
            leading=14,
        ),
        "label": ParagraphStyle(
            "PdfLabel",
            parent=base["Normal"],
            fontSize=7.5,
            textColor=TEAL,
            leading=10,
            spaceAfter=1,
            fontName="Helvetica-Bold",
        ),
        "value": ParagraphStyle(
            "PdfValue",
            parent=base["Normal"],
            fontSize=9,
            textColor=INK,
            leading=12,
        ),
        "body": ParagraphStyle(
            "PdfBody",
            parent=base["Normal"],
            fontSize=8.5,
            textColor=INK,
            leading=12,
            spaceAfter=4,
        ),
        "muted": ParagraphStyle(
            "PdfMuted",
            parent=base["Normal"],
            fontSize=8,
            textColor=MUTED,
            leading=11,
        ),
        "banner": ParagraphStyle(
            "PdfBanner",
            parent=base["Normal"],
            fontSize=8,
            textColor=MUTED,
            leading=11,
        ),
        "table_head": ParagraphStyle(
            "PdfTableHead",
            parent=base["Normal"],
            fontSize=8,
            textColor=colors.white,
            fontName="Helvetica-Bold",
            leading=11,
        ),
    }


def _empty():
    return Paragraph("&nbsp;", ParagraphStyle("PdfEmpty", fontSize=1, leading=1))


def _field(styles, label, value):
    return Paragraph(
        f'<font name="Helvetica-Bold" size="7.5" color="#0b7a75">{_escape(label).upper()}</font><br/>{_escape(value)}',
        styles["value"],
    )


def _static_image(filename, max_width=32 * mm, max_height=32 * mm):
    if not filename:
        return None
    path = IMAGES_DIR / filename
    if not path.exists():
        try:
            from django.contrib.staticfiles import finders

            found = finders.find(f"core/assets/images/{filename}")
            path = Path(found) if found else None
        except Exception:
            path = None
    if not path or not Path(path).exists():
        return None
    try:
        return _scaled_image(str(path), max_width, max_height)
    except Exception:
        return None


def _field_grid(styles, items, columns=3, width=CONTENT_COL):
    if not items:
        return Spacer(1, 1)
    col_width = width / columns
    cells = [_field(styles, label, value) for label, value in items]
    while len(cells) % columns:
        cells.append(_empty())
    rows = [cells[index:index + columns] for index in range(0, len(cells), columns)]
    table = Table(rows, colWidths=[col_width] * columns)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
    ]))
    return table


def _section(styles, title, *flowables, logo=None, top_logo=None, logo_width=32 * mm, logo_height=32 * mm):
    header = Table(
        [[Paragraph(_escape(title), styles["section"])]],
        colWidths=[PAGE_WIDTH],
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HEADER_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
    ]))
    body_content = []
    for item in flowables:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            body_content.extend([[sub] for sub in item if sub is not None])
        else:
            body_content.append([item])
    if not body_content:
        body_content = [[Spacer(1, 2)]]
    content_width = CONTENT_COL if logo else INNER_WIDTH
    content = Table(body_content, colWidths=[content_width], splitByRow=1)
    content.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    logo_img = _static_image(logo, logo_width, logo_height)
    banner_logo = _static_image(top_logo, 120 * mm, 18 * mm) if top_logo else None
    parts = []
    if banner_logo:
        parts.append(banner_logo)
    if logo_img:
        pair = Table([[logo_img, content]], colWidths=[LOGO_COL, CONTENT_COL])
        pair.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 8),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        parts.append(pair)
    else:
        parts.append(content)
    inner = Table([[part] for part in parts], colWidths=[INNER_WIDTH])
    inner.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    body = Table([[inner]], colWidths=[PAGE_WIDTH])
    body.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), BODY_PAD),
        ("RIGHTPADDING", (0, 0), (-1, -1), BODY_PAD),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [header, body, Spacer(1, 8)]


def _banner(styles, text, width=CONTENT_COL):
    table = Table(
        [[Paragraph(_escape(text), styles["banner"])]],
        colWidths=[width],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf1f3")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _qa(styles, question, answer):
    return [
        Paragraph(question, styles["body"]),
        Paragraph(_escape(answer), styles["value"]),
        Spacer(1, 4),
    ]


def _uploaded_path(uploaded):
    if not uploaded or not getattr(uploaded, "file", None):
        return ""
    try:
        path = uploaded.file.path
    except Exception:
        return ""
    return path if path and Path(path).exists() else ""


def _uploaded_name(uploaded):
    if not uploaded or not getattr(uploaded, "file", None):
        return ""
    return Path(getattr(uploaded.file, "name", "") or "").name


def _scaled_image(path, max_width, max_height):
    from PIL import Image as PilImage

    keep = BytesIO()
    with PilImage.open(path) as image:
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        converted = image.convert("RGBA" if has_alpha else "RGB")
        width, height = converted.size
        converted.save(keep, format="PNG" if has_alpha else "JPEG", quality=85)
    keep.seek(0)
    if not width or not height:
        raise ValueError("Invalid image size")
    scale = min(float(max_width) / width, float(max_height) / height)
    flowable = Image(keep, width=width * scale, height=height * scale)
    flowable._keep = keep
    return flowable


def _file_flowable(styles, uploaded, max_width=70 * mm, max_height=40 * mm):
    if not uploaded or not getattr(uploaded, "file", None):
        return Paragraph("No file uploaded", styles["muted"])
    path = _uploaded_path(uploaded)
    name = _uploaded_name(uploaded)
    ext = Path(name).suffix.lower()
    if path and ext in IMAGE_EXTS:
        try:
            return _scaled_image(path, max_width, max_height)
        except Exception:
            pass
    label = _pretty_field(getattr(uploaded, "field_name", "") or "Document")
    kind = ext.replace(".", "").upper() or "FILE"
    return Paragraph(
        f"{_escape(label)}: {_escape(name or 'Uploaded file')} ({_escape(kind)})",
        styles["muted"],
    )


def _file_card(styles, title, uploaded):
    inner = Table(
        [
            [Paragraph(_escape(title).upper(), styles["label"])],
            [_file_flowable(styles, uploaded)],
        ],
        colWidths=[None],
    )
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return inner


def _file_grid(styles, items, columns=3, width=CONTENT_COL):
    if not items:
        return Spacer(1, 1)
    col_width = width / columns
    cells = [_file_card(styles, title, uploaded) for title, uploaded in items]
    while len(cells) % columns:
        cells.append(_empty())
    rows = [cells[index:index + columns] for index in range(0, len(cells), columns)]
    table = Table(rows, colWidths=[col_width] * columns)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _first_file(files, *names):
    for name in names:
        uploaded = files.get(name)
        if uploaded:
            return uploaded
    return None


def _yes_no(first, second):
    return _text(first or second)


def _pdf_readers():
    try:
        from pypdf import PdfReader, PdfWriter
        return PdfReader, PdfWriter
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter
            return PdfReader, PdfWriter
        except ImportError:
            return None, None


def _append_uploaded_pdfs(form_pdf_bytes, uploaded_files):
    PdfReader, PdfWriter = _pdf_readers()
    if not PdfReader or not uploaded_files:
        return form_pdf_bytes

    extras = []
    for uploaded in uploaded_files:
        path = _uploaded_path(uploaded)
        name = _uploaded_name(uploaded)
        if path and Path(name).suffix.lower() == ".pdf":
            extras.append(path)
    if not extras:
        return form_pdf_bytes

    writer = PdfWriter()
    try:
        for page in PdfReader(BytesIO(form_pdf_bytes)).pages:
            writer.add_page(page)
        for path in extras:
            try:
                for page in PdfReader(path).pages:
                    writer.add_page(page)
            except Exception:
                continue
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception:
        return form_pdf_bytes


def _on_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.4)
    canvas.line(15 * mm, 10 * mm, A4[0] - 15 * mm, 10 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(15 * mm, 6.5 * mm, "Stalwart Business Solutions")
    canvas.drawRightString(A4[0] - 15 * mm, 6.5 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _build_insite(styles, app, ctx):
    pi = ctx.get("pi") or {}
    iq = ctx.get("iq") or {}
    consent = ctx.get("consent") or {}
    emp = ctx.get("emp") or {}
    ref = ctx.get("ref") or {}
    decl = ctx.get("decl") or {}
    files = ctx.get("files_by_field") or {}
    story = []
    story.extend(_section(styles, "Personal Information", _field_grid(styles, [
        ("Name", pi.get("name") or (app.candidate.full_name if app.candidate else "")),
        ("NRIC/Passport No", pi.get("passportnumber")),
        ("Nationality", pi.get("nationality")),
        ("Date of Birth", pi.get("dob")),
        ("Age", pi.get("age")),
        ("Gender", pi.get("gender")),
        ("Marital Status", pi.get("marital_status")),
    ], 3), logo="personal-details.png"))
    story.extend(_section(
        styles,
        "Address Information",
        _banner(styles, "Residential Address"),
        _field_grid(styles, [
            ("Address", pi.get("residential_address")),
            ("City", pi.get("residential_city")),
            ("State", pi.get("residential_state")),
            ("Country", pi.get("residential_country")),
            ("Pincode", pi.get("residential_pincode")),
        ], 3),
        _banner(styles, "Permanent Address"),
        _field_grid(styles, [
            ("Address", pi.get("permanent_address")),
            ("City", pi.get("permanent_city")),
            ("State", pi.get("permanent_state")),
            ("Country", pi.get("permanent_country")),
            ("Pincode", pi.get("permanent_pincode")),
        ], 3),
        logo="address.png",
    ))
    story.extend(_section(styles, "Personal Health Metrics", _field_grid(styles, [
        ("Height (CMS)", f"{pi.get('height')} CMS" if pi.get("height") else "—"),
        ("Weight (KG)", f"{pi.get('weight')} KG" if pi.get("weight") else "—"),
        ("BMS (Body Mass Index)", pi.get("bms")),
    ]), logo="personal-health-metrics.png"))
    story.extend(_section(styles, "Contact Information", _field_grid(styles, [
        ("Email Address", pi.get("email") or (app.candidate.email if app.candidate else "")),
        ("Phone Number", pi.get("phone") or (app.candidate.phone if app.candidate else "")),
    ], 2), logo="contact-information.png"))
    story.extend(_section(styles, "Others", _field_grid(styles, [
        ("Do you have any special medical condition?", pi.get("medical_display")),
        ("Have you been convicted of a criminal offence (felony or mis-demeanour)?", pi.get("criminal_display")),
    ], 2), logo="others.png"))
    story.extend(_section(
        styles,
        "IQ Test",
        Paragraph(
            "Participation in the IQ test is part of our evaluation process to assess cognitive abilities and problem-solving skills relevant to the position.",
            styles["muted"],
        ),
    ))
    story.extend(_section(styles, "Part A: IQ Test",
        _qa(styles, "<b>1.</b> At a conference, 12 members shook hands with each other before &amp; after the meeting. How many total numbers of handshakes occurred?", iq.get("iq_q1")),
        _qa(styles, "<b>2 (a).</b> A fisherman has 5 fishes (namely A, B, C, D, E) each having a different weight.", iq.get("iq_q2")),
        _qa(styles, "<b>2 (b).</b> Which of the following is the lightest?", iq.get("iq_q3")),
        _qa(styles, "<b>3.</b> Which number should come next in the series 1, 3, 6, 10, 15, ......", iq.get("iq_q4")),
        _qa(styles, "<b>4.</b> If 10 people can do a piece of work in 5 days, working 2 hours a day; how long will 2 people take to do the same work, working 5 hours a day?", iq.get("iq_q5")),
        _qa(styles, "<b>5.</b> 87 kg of potatoes are distributed in two boxes. One box weighs 11 kg less than the other one. How many kilograms of potatoes does the lighter box contain?", iq.get("iq_response_1")),
        logo="iq-test-1.png",
    ))
    story.extend(_section(styles, "Part B: Programming Test",
        _qa(styles, '<b>1.</b> Please write a simple module using "do while…. Loop" to display the following result:', iq.get("iq_response_2")),
        logo="iq-test-2.png",
    ))
    story.extend(_section(styles, "Part C: SQL Test",
        _qa(styles, '<b>1.</b> Please write the SQL statement to display record with "Title" as "Pn".', iq.get("iq_response_3")),
        _qa(styles, '<b>2.</b> Please write the SQL statement to display "Customer" name start with "H".', iq.get("iq_response_4")),
        _qa(styles, '<b>3.</b> Please write the SQL statement to find the sum of all "OrderPrice" fields group individual customer.', iq.get("iq_response_5")),
        _qa(styles, '<b>4.</b> Please write the SQL statement to display the records for "OrderDate" is belong to year 2009 only.', iq.get("iq_response_6")),
        _qa(styles, '<b>5.</b> Please write the SQL statement to display "Customer" name only and this query must avoid retrieving duplicate "Customer" name.', iq.get("iq_response_7")),
        logo="iq-test-3.png",
    ))
    story.extend(_section(styles, "Consent Form",
        Paragraph(
            f'I <b>{_escape(consent.get("consentname") or (app.candidate.full_name if app.candidate else ""))}</b> hereby authorize Insite MY Systems Sdn Bhd (the "Company") of Unit 17-11, Level 17, Tower A, Vertical Business Suite, Avenue 3 Bangsar South, No 8 Jalan Kerinchi, 59200 Kuala Lumpur. A telephone facsimile (fax) or scan copies of this consent shall be considered as valid as the original consent.',
            styles["body"],
        ),
        Paragraph("I hereby consent to the Company's verification of all the information I have provided on my application form, medical histories, background records, and CTOS checks on a continuing basis.", styles["body"]),
        _field_grid(styles, [
            ("Applicant Name", consent.get("appname") or (app.candidate.full_name if app.candidate else "")),
            ("Applicant NRIC", consent.get("nric") or pi.get("passportnumber")),
            ("Dated", consent.get("date")),
        ]),
        logo="consent-form.png",
        top_logo="insitemy-consent-logo.png",
    ))
    story.extend(_section(
        styles,
        "Documents Submission",
        _file_grid(styles, [
            ("Personality Test Result", files.get("personality_test_result")),
            ("Updated Resume", files.get("updated_resume")),
            ("MyKad/NRIC/Passport", files.get("identity_document")),
            ("Academic Certificate", files.get("academic_certificate")),
        ], 3, width=INNER_WIDTH),
        _banner(styles, "Last 3 Months Payslip", width=INNER_WIDTH),
        _file_grid(styles, [
            ("Pay Slip 1", files.get("payslips") or files.get("payslip") or files.get("payslip_1")),
            ("Pay Slip 2", files.get("payslip_2") or files.get("payslips_2")),
            ("Pay Slip 3", files.get("payslip_3") or files.get("payslips_3")),
        ], 3, width=INNER_WIDTH),
    ))
    story.extend(_section(styles, "Employment Expectations", _field_grid(styles, [
        ("Willing to Travel", emp.get("willing_to_travel")),
        ("Willing to Relocate", emp.get("willing_to_relocate")),
        ("Possess Own Transport", emp.get("own_transport")),
        ("Last Drawn Salary (RM)", emp.get("lastsalary")),
        ("Expected Salary (RM)", emp.get("expectedsalary")),
        ("Notice Period / Availability", emp.get("notice_period")),
        ("If you are refer by an Stalwart Business Solutions staff, please state his/her name", emp.get("stalwart_referral")),
    ]), logo="employment-expectations.png"))
    story.extend(_section(
        styles,
        "References Details",
        _banner(styles, "Reference 1"),
        _field_grid(styles, [
            ("Name", ref.get("ref1name")),
            ("Position", ref.get("ref1pos")),
            ("Company", ref.get("ref1com")),
            ("Email Address", ref.get("ref1email")),
            ("Phone Number", ref.get("ref1phn")),
        ]),
        _banner(styles, "Reference 2"),
        _field_grid(styles, [
            ("Name", ref.get("ref2name")),
            ("Position", ref.get("ref2pos")),
            ("Company", ref.get("ref2com")),
            ("Email Address", ref.get("ref2email")),
            ("Phone Number", ref.get("ref2phn")),
        ]),
        logo="reference-details.png",
    ))
    story.extend(_section(styles, "Applicant Declaration",
        Paragraph("I certify that information contained in this application is true and complete. I understand that false information may be grounds for not hiring me or for immediate termination of the employment at any point in the future if I am hired. I authorize the verification of any or all information listed above.", styles["body"]),
        _field_grid(styles, [
            ("Applicant's Name", decl.get("declaration_appname") or decl.get("appname") or (app.candidate.full_name if app.candidate else "")),
            ("Applicant's Place", decl.get("appplace")),
            ("Date Applied", decl.get("dateapplied")),
        ], width=INNER_WIDTH),
        _file_card(styles, "Applicant's Signatory", files.get("applicant_signature")),
    ))
    return story

def build_application_pdf(app, ctx):
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Application {app.id}",
    )

    styles = _styles()

    company = (
        app.company.name if app.company else "Application"
    ).strip()

    candidate = (
        app.candidate.full_name
        if app.candidate
        else "Candidate"
    )

    company_lower = company.lower()

    story = [
        Paragraph(
            "Stalwart Business Solutions",
            styles["brand"],
        ),
        Paragraph(
            f"{_escape(company)} Job Application",
            styles["subtitle"],
        ),
        Paragraph(
            f"#{app.id} &nbsp;&nbsp; "
            f"{_escape(ctx.get('occupation_label') or 'Application')} "
            f"&nbsp;&nbsp; "
            f"{_escape(candidate)} &nbsp;&nbsp; "
            f"{_escape(app.candidate.email if app.candidate else '')} "
            f"&nbsp;&nbsp; "
            f"Status: {_escape(app.get_status_display())}",
            styles["meta"],
        ),
    ]

    if "nevigate" in company_lower:
        story.extend(
            _build_nevigate(styles, app, ctx)
        )

    elif "insite" in company_lower:
        story.extend(
            _build_insite(styles, app, ctx)
        )

    else:
        story.extend(
            _build_macro(styles, app, ctx)
        )

    doc.build(
        story,
        onFirstPage=_on_page,
        onLaterPages=_on_page,
    )

    pdf_bytes = buffer.getvalue()

    response = HttpResponse(
        pdf_bytes,
        content_type="application/pdf",
    )

    response["Content-Disposition"] = (
        f'attachment; filename="application_{app.id}.pdf"'
    )

    return response

def _build_macro(styles, app, ctx):
    fields = ctx.get("fields") or {}
    files = ctx.get("files_by_field") or {}
    candidate_name = app.candidate.full_name if app.candidate else ""
    story = []
    photo = _file_flowable(
        styles,
        _first_file(files, "macro_doc_1_1", "passport_photo"),
        28 * mm,
        36 * mm,
    )
    photo_block = Table(
        [
            [Paragraph("PASSPORT SIZE PHOTO", styles["label"])],
            [photo],
        ],
        colWidths=[CONTENT_COL],
    )
    photo_block.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
    ]))
    story.extend(_section(
        styles,
        "Personal Particulars",
        photo_block,
        _field_grid(styles, [
            ("Name", fields.get("name") or candidate_name),
        ], 1),
        _field_grid(styles, [
            ("Permanent Home Address", fields.get("address")),
        ], 1),
        _field_grid(styles, [
            ("Current Home Address", fields.get("address_2")),
        ], 1),
        _field_grid(styles, [
            ("Date of Birth", fields.get("dob")),
            ("Age", fields.get("age")),
            ("Gender", fields.get("gender")),
            ("Place of Birth", fields.get("dob_2")),
        ], 4),
        _field_grid(styles, [
            ("Email Address", fields.get("email") or (app.candidate.email if app.candidate else "")),
        ], 1),
        _field_grid(styles, [
            ("Identity Card No.", fields.get("passportnumber")),
            ("Marital Status", fields.get("marital")),
            ("Nationality", fields.get("nationality")),
        ], 3),
        _field_grid(styles, [
            ("House Phone No.", fields.get("nationality_2")),
            ("Office Phone No.", fields.get("nationality_3")),
            ("Handphone No.", fields.get("nationality_4")),
        ], 3),
        logo="personal-details.png",
    ))
    story.extend(_section(styles, "Family Information (Parents, Brothers, Sisters, Spouse, Children)", _field_grid(styles, [
        ("Father", fields.get("father")),
        ("Mother", fields.get("mother")),
        ("Spouse", fields.get("spouse")),
        ("Children", fields.get("children")),
        ("Brothers", fields.get("brothers")),
        ("Sisters", fields.get("sisters")),
        ("How do you stay?", ctx.get("residence_label")),
    ]), logo="family.png"))
    story.extend(_section(styles, "Employment History", _field_grid(styles, [
        ("Date From", fields.get("datefrom")),
        ("Date To", fields.get("dateto")),
        ("Name of Employer", fields.get("height")),
        ("Location", fields.get("location")),
        ("Name of Immediate Superior", fields.get("superior")),
        ("Position", fields.get("position")),
        ("Start Salary", fields.get("ssalary")),
        ("End Salary", fields.get("esalary")),
        ("Reasons For Leaving", fields.get("reasons")),
        ("Brief Job Description", fields.get("macro_2_text_1") or fields.get("reasons")),
    ], 2), logo="experience.png"))
    story.extend(_section(styles, "Education", _field_grid(styles, [
        ("College / University", fields.get("height_2")),
        ("Location", fields.get("location_2") or fields.get("location")),
        ("Year From", fields.get("yearfrom")),
        ("Year To", fields.get("yearto")),
        ("Qualification", fields.get("qualification")),
    ]), logo="college.png"))
    story.extend(_section(styles, "Continuing education you are currently attending (If any)", _field_grid(styles, [
        ("College / University", fields.get("height_2")),
        ("Year Start", fields.get("yearfrom_2")),
        ("Year End", fields.get("yearto_2")),
        ("Qualification", fields.get("qualification_2") or fields.get("qualification")),
    ]), logo="education.png"))
    story.extend(_section(styles, "Membership", _field_grid(styles, [
        ("Membership (Professional Bodies, Social, Sport etc)", fields.get("height_3")),
    ], 1), logo="membership.png"))
    story.extend(_section(styles, "Courses", _field_grid(styles, [
        ("Major Courses Attended (with date)", fields.get("height_4")),
    ], 1), logo="certificate.png"))
    language_rows = [
        [
            Paragraph("Language / Dialect", styles["table_head"]),
            Paragraph("Speak", styles["table_head"]),
            Paragraph("Read", styles["table_head"]),
            Paragraph("Write", styles["table_head"]),
        ],
        [Paragraph("English", styles["value"]), Paragraph(_escape(fields.get("lang")), styles["value"]), Paragraph(_escape(fields.get("lang_2")), styles["value"]), Paragraph(_escape(fields.get("lang_3")), styles["value"])],
        [Paragraph("Malay", styles["value"]), Paragraph(_escape(fields.get("lang_4")), styles["value"]), Paragraph(_escape(fields.get("lang_5")), styles["value"]), Paragraph(_escape(fields.get("lang_6")), styles["value"])],
        [Paragraph("Chinese", styles["value"]), Paragraph(_escape(fields.get("lang_7")), styles["value"]), Paragraph(_escape(fields.get("lang_8")), styles["value"]), Paragraph(_escape(fields.get("lang_9")), styles["value"])],
        [Paragraph("Tamil", styles["value"]), Paragraph(_escape(fields.get("lang_10")), styles["value"]), Paragraph(_escape(fields.get("lang_11")), styles["value"]), Paragraph(_escape(fields.get("lang_12")), styles["value"])],
        [Paragraph(_escape(fields.get("lang_other") or fields.get("others") or "Others"), styles["value"]), Paragraph(_escape(fields.get("lang_13")), styles["value"]), Paragraph(_escape(fields.get("lang_14")), styles["value"]), Paragraph(_escape(fields.get("lang_15")), styles["value"])],
        [Paragraph(_escape(fields.get("lang_other_2") or "Others"), styles["value"]), Paragraph(_escape(fields.get("lang_16")), styles["value"]), Paragraph(_escape(fields.get("lang_17")), styles["value"]), Paragraph(_escape(fields.get("lang_18")), styles["value"])],
    ]
    language_col = CONTENT_COL / 4
    language_table = Table(language_rows, colWidths=[language_col] * 4)
    language_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.3, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.extend(_section(styles, "Language / Dialect", language_table, logo="language.png"))
    story.extend(_section(styles, "Activities / Interest / Hobbies", _field_grid(styles, [
        ("Activities / Interest / Hobbies", fields.get("height_5")),
    ], 1), logo="hobbies.png"))
    story.extend(_section(styles, "General Information", _field_grid(styles, [
        ("Do you have any physical impairment?", _yes_no(fields.get("macro_4_checkbox_1"), fields.get("macro_4_checkbox_2"))),
        ("Have you had any serious or contagious illness prior to this application?", _yes_no(fields.get("macro_4_checkbox_3"), fields.get("macro_4_checkbox_4"))),
        ("Have you ever been in the service of this Company or its subsidiaries?", _yes_no(fields.get("macro_4_checkbox_5"), fields.get("macro_4_checkbox_6"))),
        ("Are you related to anyone working in this Company or its subsidiaries?", _yes_no(fields.get("macro_4_checkbox_7"), fields.get("macro_4_checkbox_8"))),
        ("Have you ever been dismissed or suspended from any position?", _yes_no(fields.get("macro_4_checkbox_9"), fields.get("macro_4_checkbox_10"))),
        ("Have you ever been convicted of criminal offences in the Court of Law?", _yes_no(fields.get("macro_4_checkbox_11"), fields.get("macro_4_checkbox_12"))),
        ("Have you ever been declared bankrupt?", _yes_no(fields.get("macro_4_checkbox_13"), fields.get("macro_4_checkbox_14"))),
        ("Are you currently engaged in any private businesses?", _yes_no(fields.get("macro_4_checkbox_15"), fields.get("macro_4_checkbox_16"))),
        ("Have you ever been involved with the use of any illegal drugs?", _yes_no(fields.get("macro_4_checkbox_17"), fields.get("macro_4_checkbox_18"))),
        ("Do you have a valid driving licence? What class?", _yes_no(fields.get("macro_4_checkbox_19"), fields.get("macro_4_checkbox_20"))),
    ], 2), logo="others.png"))
    story.extend(_section(styles, "Availability", _field_grid(styles, [
        ("By which date would you be available to take up this new appointment?", fields.get("lastsalary")),
        ("Expected Salary", fields.get("lastsalary_2")),
        ("Expected Benefits", fields.get("expectedsalary")),
    ], 2), logo="interview.png"))
    story.extend(_section(
        styles,
        "References Details",
        _banner(styles, "Reference 1"),
        _field_grid(styles, [
            ("Name of Referee", fields.get("ref1name")),
            ("Occupation", fields.get("ref1pos")),
            ("Name of Company", fields.get("ref1com")),
            ("Email Address", fields.get("ref1email")),
            ("Phone Number", fields.get("ref1phn")),
        ]),
        _banner(styles, "Reference 2"),
        _field_grid(styles, [
            ("Name of Referee", fields.get("ref1name_2")),
            ("Occupation", fields.get("ref1pos_2")),
            ("Name of Company", fields.get("ref1com_2")),
            ("Email Address", fields.get("ref1email_2")),
            ("Phone Number", fields.get("ref1phn_2")),
        ]),
        logo="reference-details.png",
    ))
    story.extend(_section(styles, "Applicant Declaration",
        Paragraph("The particulars which I have stated are true and correct to the best of my knowledge and made on my own behalf. Any false or inaccurate information given will render any subsequent employment contract null and void.", styles["body"]),
        _field_grid(styles, [
            ("Signature of Applicant", fields.get("appname") or candidate_name),
            ("Date", fields.get("dateapplied") or fields.get("date")),
        ], 2, width=INNER_WIDTH),
    ))
    story.extend(_section(styles, "MACROKIOSK Personal Data Consent for Job Applicants",
        Paragraph(
            f'I <b>{_escape(fields.get("macro_5_yourname") or fields.get("name") or candidate_name)}</b>, NRIC/Passport <b>{_escape(fields.get("macro_5_nric") or fields.get("passportnumber"))}</b> hereby grant consent to VENOVOX SDN BHD to conduct background screening as part of the recruitment process.',
            styles["body"],
        ),
        _field_grid(styles, [
            ("Candidate's Name", fields.get("macro_5_name") or fields.get("name") or candidate_name),
            ("Date", fields.get("macro_5_date") or fields.get("dateapplied") or fields.get("date")),
            ("NRIC/Passport No.", fields.get("nric") or fields.get("passportnumber")),
        ], width=INNER_WIDTH),
        _file_card(
            styles,
            "Candidate's Signature",
            _first_file(files, "macro_doc_5_1", "applicant_signature"),
        ),
    ))
    story.extend(_section(styles, "General Assignment",
        Paragraph("If you are selected for the applied position, please share with us on what you can contribute to MACROKIOSK.", styles["body"]),
        Paragraph(_escape(fields.get("macro_6_text_1")), styles["value"]),
        logo="writing.png",
    ))
    story.extend(_section(styles, "HR PDPA Consent Form",
        Paragraph("The information you provide will be used and processed by MACROKIOSK for purposes related to your registration of interest and the processing of your application for employment. By giving your consent, you agree to your data being used in conjunction with recruitment and employment purposes.", styles["body"]),
        _field_grid(styles, [
            ("Name", fields.get("macro_7_name") or fields.get("name") or candidate_name),
            ("NRIC/ID No.", fields.get("macro_7_nric") or fields.get("nric") or fields.get("passportnumber")),
            ("Date", fields.get("macro_7_date") or fields.get("dateapplied") or fields.get("date")),
        ]),
        logo="contract.png",
    ))
    return story
company = (
        app.company.name if app.company else "Application"
    ).strip()

candidate = (
        app.candidate.full_name if app.candidate else "Candidate"
    )

company_lower = company.lower()

story = [
        Paragraph("Stalwart Business Solutions", styles["brand"]),
        Paragraph(
            f"{_escape(company)} Job Application",
            styles["subtitle"],
        ),
        Paragraph(
            f"#{app.id} &nbsp;&nbsp; "
            f"{_escape(ctx.get('occupation_label') or 'Application')} "
            f"&nbsp;&nbsp; {_escape(candidate)} "
            f"&nbsp;&nbsp; "
            f"{_escape(app.candidate.email if app.candidate else '')} "
            f"&nbsp;&nbsp; "
            f"Status: {_escape(app.get_status_display())}",
            styles["meta"],
        ),
    ]

if "nevigate" in company_lower:
        story.extend(_build_nevigate(styles, app, ctx))

elif "insite" in company_lower:
        story.extend(_build_insite(styles, app, ctx))

else:
        story.extend(_build_macro(styles, app, ctx))



def _build_nevigate(styles, app, ctx):
    """
    Build the Nevigate PDF using only values submitted through
    the Nevigate application form.

    Empty or missing values remain blank.
    """
    data = app.data if isinstance(app.data, dict) else {}
    nv = data.get("nevigate_application", {})

    if not isinstance(nv, dict):
        nv = {}

    story = []

    def value(key):
        raw = nv.get(key, "")
        if raw is None:
            return ""
        return str(raw).strip()

    def paragraph(text, style):
        # Preserve blank values instead of displaying placeholder text.
        return Paragraph(xml_escape(str(text)) if text else " ", style)

    def field_table(rows):
        table_data = []

        for label, key in rows:
            field_value = value(key)

            table_data.append([
                paragraph(label, styles["label"]),
                paragraph(field_value, styles["value"]),
            ])

        if not table_data:
            return []

        table = Table(
            table_data,
            colWidths=[CONTENT_COL * 0.38, CONTENT_COL * 0.62],
            hAlign="LEFT",
        )

        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, -1), 0.35, LINE),
        ]))

        return [table]

    # Application details
    story.extend(_section(
        styles,
        "Application Details",
        field_table([
            ("Company Name", "company_name"),
        ]),
    ))

    # Further education
    story.extend(_section(
        styles,
        "Further Education",
        field_table([
            ("Currently Pursuing Education", "pursuing_edu"),
            ("Future Education Plans", "future_edu"),
        ]),
    ))

    # Language skills
    story.extend(_section(
        styles,
        "Language Skills",
        field_table([
            ("English — Speaking", "sp_eng"),
            ("English — Writing", "wr_eng"),
            ("Bahasa Malaysia — Speaking", "sp_bm"),
            ("Bahasa Malaysia — Writing", "wr_bm"),
            ("Mandarin — Speaking", "sp_mand"),
            ("Mandarin — Writing", "wr_mand"),
        ]),
    ))

    # Computer skills
    story.extend(_section(
        styles,
        "Computer Skills",
        field_table([
            ("Microsoft Word", "ms_word"),
            ("Microsoft Excel", "ms_excel"),
            ("Microsoft PowerPoint", "ms_ppt"),
            ("Microsoft Access", "ms_access"),
            ("Email / Internet", "email_net"),
        ]),
    ))

    # Declarations
    story.extend(_section(
        styles,
        "Declarations",
        field_table([
            ("Own Transportation", "own_trans"),
            ("Willing to Travel / Relocate", "travel_reloc"),
            ("Criminal Offence Declaration", "criminal"),
            ("Disciplinary Declaration", "disciplinary"),
            ("Medical Condition Declaration", "medical"),
        ]),
    ))

    # Signatures
    signature_rows = []

    for label, key in [
        ("Application Signature", "app_signature_data"),
        ("PDPA Consent Signature", "pdpa_signature_data"),
    ]:
        raw_value = value(key)

        if raw_value.startswith("data:image/") and "," in raw_value:
            try:
                image_data = raw_value.split(",", 1)[1]
                decoded_image = base64.b64decode(image_data)

                signature_image = Image(
                    BytesIO(decoded_image),
                    width=35 * mm,
                    height=14 * mm,
                    kind="proportional",
                )

                signature_rows.append([
                    paragraph(label, styles["label"]),
                    signature_image,
                ])

            except (ValueError, TypeError):
                signature_rows.append([
                    paragraph(label, styles["label"]),
                    paragraph("", styles["value"]),
                ])
        else:
            signature_rows.append([
                paragraph(label, styles["label"]),
                paragraph("", styles["value"]),
            ])

    story.extend(_section(
        styles,
        "Signatures & Consent",
        [
            Table(
                signature_rows,
                colWidths=[CONTENT_COL * 0.38, CONTENT_COL * 0.62],
                hAlign="LEFT",
                style=TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]),
            )
        ],
    ))

    return story