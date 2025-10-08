#!/usr/bin/env python3
"""
Generate UPSC PDF and email it.
This script:
 - Creates UPSC_current_affairs_detailed_YYYY-MM-DD.pdf in the repo workspace
 - Emails it using SMTP credentials provided via environment variables

Required env vars (set as repo Secrets):
 - SMTP_HOST (default smtp.gmail.com)
 - SMTP_PORT (default 587)
 - SMTP_USER
 - SMTP_PASSWORD
 - EMAIL_TO
Optionally:
 - PDF_PATH (overrides generated file path)
"""

import os, datetime, ssl, smtplib
from email.message import EmailMessage

# ---- PDF generation (reportlab) ----
def generate_pdf(output_path):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.units import mm
    except Exception as e:
        raise RuntimeError("reportlab must be installed. Add 'pip install reportlab' to workflow.") from e

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='MyTitle', fontSize=16, leading=18, spaceAfter=8, spaceBefore=8))
    styles.add(ParagraphStyle(name='MyHeading', fontSize=13, leading=15, spaceAfter=6, spaceBefore=10))
    styles.add(ParagraphStyle(name='MyBody', fontSize=10, leading=13))

    content = []
    now = datetime.datetime.now().strftime("%d %B %Y %H:%M:%S")
    content.append(Paragraph("UPSC Current Affairs — Detailed Brief Insights (" + datetime.datetime.now().strftime("%d %B %Y") + ")", styles['MyTitle']))
    content.append(Paragraph("Concise but comprehensive insights across all major UPSC subjects. Each item includes a short analysis and exam relevance.", styles['MyBody']))
    content.append(Spacer(1,8))
    content.append(Paragraph(f"Compiled on {now}.", styles['MyBody']))
    content.append(Spacer(1,8))

    # Example sections: replace / extend with your current content as needed
    sections = {
        "Polity & International Relations": "PM Modi spoke with Russian President Vladimir Putin to reaffirm the 'Special and Privileged Strategic Partnership'. They discussed defence and energy cooperation. Significance: India's diplomacy with major powers and energy security.",
        "Economy & Finance": "RBI defended the rupee near ~₹88.80/USD via market intervention. Traders report some Russian oil payments being settled in yuan, affecting forex patterns.",
        "Environment & Disaster Management": "A landslide in Himachal Pradesh's Bilaspur district killed several people after heavy rain; highlights disaster readiness and climate-related vulnerabilities.",
        "Defence & Internal Security": "Ongoing defence cooperation and NSG preparedness exercises; internal security concerns continue in affected regions.",
        "Ethics, Integrity & Governance": "Debates on misinformation regulation vs free speech; corporate governance issues in trustee disputes raised transparency questions.",
        "Agriculture & Rural Development": "Flood impacts and MSP changes focus on food security, procurement, and rural relief measures.",
        "Science & Technology / Space": "ISRO infrastructure updates and public interest events like supermoon; space policy implications remain relevant.",
        "Culture & Heritage": "INTACH appointments and heritage protection debates; interplay of community rights and conservation.",
        "Urban Governance & Environment": "Urban greening initiatives and municipal capacity are central to pollution control and governance."
    }

    for title, body in sections.items():
        content.append(Paragraph(title, styles['MyHeading']))
        content.append(Paragraph(body, styles['MyBody']))
        content.append(Spacer(1,8))

    content.append(Paragraph("Sources: Reuters, AP, PMO/PMIndia, ISRO, The Hindu, PIB (check official releases for details).", styles['MyBody']))
    content.append(Spacer(1,12))

    doc.build(content)

# ---- Email sending ----
def send_email(pdf_path):
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO")

    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("Missing SMTP_USER, SMTP_PASSWORD or EMAIL_TO environment variables.")

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found at {pdf_path}")

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = "UPSC Current Affairs — Detailed Brief (" + datetime.datetime.now().strftime("%d %b %Y") + ")"
    msg.set_content("Please find attached the UPSC current affairs detailed brief.\n\nRegards,\nUPSC Brief Service")

    with open(pdf_path, "rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---- Main ----
if __name__ == "__main__":
    # Generated filename using today's date
    default_name = "UPSC_current_affairs_detailed_" + datetime.datetime.now().strftime("%Y-%m-%d") + ".pdf"
    PDF_PATH = os.environ.get("PDF_PATH", default_name)

    # Generate PDF
    print("Generating PDF at:", PDF_PATH)
    generate_pdf(PDF_PATH)
    print("PDF generated.")

    # Send it
    print("Sending email...")
    send_email(PDF_PATH)
    print("Done.")