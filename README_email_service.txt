
README - Email PDF Service (UPSC brief)
Files:
- UPSC_current_affairs_detailed_2025-10-08.pdf  (the generated PDF)
- email_pdf_service.py                          (Python script to send the PDF via SMTP)

Quick setup:
1. Move files to a server or your local machine with Python 3.8+ installed.
2. Configure environment variables (recommended) or edit the top of email_pdf_service.py:
   - SMTP_HOST (e.g., smtp.gmail.com)
   - SMTP_PORT (e.g., 587)
   - SMTP_USER (your email)
   - SMTP_PASSWORD (app password or SMTP password)
   - EMAIL_TO (recipient email)
   - PDF_PATH (path to the PDF file)

Security note:
- For Gmail, enable 2FA and create an App Password; do NOT store plain credentials in code.
- Use environment variables or a secrets manager (AWS Secrets Manager, HashiCorp Vault) in production.

Scheduling options:
- Cron (Linux): `0 7 * * * /usr/bin/python3 /path/to/email_pdf_service.py`  (sends daily at 07:00)
- systemd timer: create a systemd service + timer unit that runs the script daily.
- GitHub Actions: create a workflow that runs daily and uses secrets to store SMTP creds.

If you want, I can:
- Add SendGrid / Mailgun support (API-based) instead of SMTP.
- Provide a systemd unit file and an example cron entry.
- Help you configure this on a small VM and test sending (you'd need to provide SMTP creds securely).
