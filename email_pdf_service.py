#!/usr/bin/env python3
"""
email_pdf_service.py
Generates a template-styled UPSC current-affairs PDF (based on user demo layout)
and emails it via SMTP. Designed to run inside GitHub Actions.

Required repo secrets:
 - SMTP_USER
 - SMTP_PASSWORD   (use Gmail App Password if using Gmail)
 - EMAIL_TO
Optional:
 - SMTP_HOST (default smtp.gmail.com)
 - SMTP_PORT (default 587)

Commit this file to the repo root and use the workflow YAML below.
"""
import os, datetime, ssl, smtplib, textwrap
from email.message import EmailMessage

# ---------- PDF generation ----------
def generate_pdf(path):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.units import mm
    except Exception as e:
        raise RuntimeError("reportlab not installed. Ensure 'pip install reportlab' runs in the workflow.") from e

    doc = SimpleDocTemplate(path, pagesize=A4,
                            rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleLarge', fontSize=16, leading=18, spaceAfter=6, spaceBefore=6))
    styles.add(ParagraphStyle(name='Section', fontSize=12, leading=14, spaceAfter=4, spaceBefore=8))
    styles.add(ParagraphStyle(name='Body', fontSize=10, leading=12))
    styles.add(ParagraphStyle(name='Small', fontSize=9, leading=11))

    content = []
    today_str = datetime.datetime.now().strftime("%d %B %Y")
    content.append(Paragraph(f"UPSC CURRENT AFFAIRS – {today_str}", styles['TitleLarge']))
    content.append(Paragraph("Daily insights | Template-based layout (user demo)", styles['Small']))
    content.append(Spacer(1,8))

    # Template sections derived from the demo you uploaded
    sections = [
        ("GS Paper 2", [
            ("The State of Social Justice 2025",
             "Context: ILO released 'The State of Social Justice 2025' assessing progress since 1995. Key points: four foundational pillars; progress in poverty reduction; persistent inequalities and recommended policy actions."),
        ]),
        ("GS Paper 4", [
            ("Passive Euthanasia in India",
             "Context and summary: legal judgments (Aruna Shanbaug 2011, Common Cause 2018), advance directives, medical board oversight, ethical dilemmas and proposed reforms such as digital advance directives and hospital ethics committees."),
        ]),
        ("Content for Mains Enrichment (CME)", [
            ("Indian Diet",
             "ICMR-INDIAB 2025 shows high carbohydrate share in Indian diets; links to NCDs and policy implications for Poshan 2.0 and public health."),
        ]),
        ("Facts for Prelims (FFP)", [
            ("Nobel Medicine Prize 2025",
             "Winners and short explanation: Tregs and peripheral immune tolerance — implications for cancer, autoimmunity and transplantation."),
            ("International Stabilization Force for Gaza",
             "Short explainer: proposed multinational force model under US oversight; functions and geopolitical implications."),
        ]),
        ("Mapping", [
            ("Port of Pasni",
             "Short explainer: Pakistan proposes Pasni for mineral export; strategic implications in Arabian Sea and relations with Chabahar/Gwadar."),
        ])
    ]

    for sec_title, items in sections:
        content.append(Paragraph(sec_title, styles['Section']))
        for title, body in items:
            content.append(Paragraph(f"<b>{title}</b>", styles['Body']))
            wrapped = textwrap.fill(body, 110)
            for para in wrapped.split("\n"):
                content.append(Paragraph(para, styles['Body']))
            content.append(Spacer(1,6))
        content.append(Spacer(1,8))

    content.append(Paragraph("Sources: sample placeholders (InsightsonIndia demo, The Hindu, Reuters, AP, PIB). Replace with live-scraped or curated sources as needed.", styles['Small']))
    content.append(Spacer(1,6))
    content.append(Paragraph(f"Prepared automatically — template adapted from user demo. Generated on {today_str}.", styles['Small']))

    doc.build(content)

# ---------- Email sending ----------
def send_email(pdf_path):
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO")

    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("Missing required environment variables: SMTP_USER, SMTP_PASSWORD, EMAIL_TO")

    if not os.path.exists(pdf_path):
        generate_pdf(pdf_path)

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = "UPSC Current Affairs — Daily Brief (" + datetime.datetime.now().strftime("%d %b %Y") + ")"
    msg.set_content("Attached: UPSC current affairs brief (template-based).")

    with open(pdf_path, "rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------- Main ----------
if __name__ == "__main__":
    name = "UPSC_current_affairs_template_based_" + datetime.datetime.now().strftime("%Y-%m-%d") + ".pdf"
    path = os.environ.get("PDF_PATH", name)
    print("Generating PDF at:", path)
    generate_pdf(path)
    print("Sending email...")
    send_email(path)
    print("Done.")