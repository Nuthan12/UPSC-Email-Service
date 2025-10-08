#!/usr/bin/env python3
"""
email_pdf_service.py
Simple script to email the generated UPSC PDF to a recipient using SMTP.
Configure the variables below and run:
    python3 email_pdf_service.py
For repeated/daily sending, schedule via cron or systemd timers (instructions in README_email_service.txt).
"""
import smtplib, ssl, os, datetime
from email.message import EmailMessage

# CONFIG - edit these or set as environment variables
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "your.email@example.com")  # your SMTP username
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "your_app_password")  # app password or SMTP password
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO", "recipient@example.com")
SUBJECT = "UPSC Current Affairs — Detailed Brief (" + datetime.datetime.now().strftime("%d %b %Y") + ")"
PDF_PATH = os.environ.get("PDF_PATH", "UPSC_current_affairs_detailed_2025-10-08.pdf")

def send_email():
    if not os.path.exists(PDF_PATH):
        raise FileNotFoundError(f"PDF not found at {PDF_PATH}")
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = SUBJECT
    msg.set_content("Please find attached the UPSC current affairs detailed brief.\n\nRegards,\nYour UPSC Brief Service")
    with open(PDF_PATH, "rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=os.path.basename(PDF_PATH))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("Email sent to", EMAIL_TO)

if __name__ == '__main__':
    send_email()
