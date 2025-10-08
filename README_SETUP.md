# UPSC Automated AI-powered Daily Brief (ready-to-upload)

This repository contains an automated pipeline (GitHub Actions) that fetches news, uses OpenAI to classify and summarize
articles in UPSC style (InsightsonIndia-like depth), generates a templated PDF, and emails it every morning at 08:30 IST.

## Files included
- generate_and_send.py  - main script (AI classification + summarization + PDF + email)
- .github/workflows/daily-upsc.yml - GitHub Actions workflow (runs daily at 08:30 IST)


## Setup steps (do this from GitHub web UI on your phone)
1. Create a new repo or use your existing one.
2. Upload these files to the repo root and commit to `main` (the workflow file must be at `.github/workflows/daily-upsc.yml`).
3. Add repository secrets (Settings → Secrets and variables → Actions):
   - `OPENAI_API_KEY`  (your OpenAI API key)
   - `SMTP_USER`       (email used to send)
   - `SMTP_PASSWORD`   (app password for Gmail or SMTP password)
   - `EMAIL_TO`        (recipient email)

Optional: set `OPENAI_MODEL` to a preferred model name (default: gpt-4o-mini).

## Testing
- In Actions → choose 'Send Daily UPSC AI Brief' → click 'Run workflow' → select `main` and run.
- Check logs; if successful you will receive the email with the generated PDF.

## Notes
- Some news sites block scraping; you can add/remove RSS feeds in `generate_and_send.py`.
- For high-quality summaries, provide a valid `OPENAI_API_KEY` (billing may apply).
- Use Gmail App Passwords for `SMTP_PASSWORD` if using Gmail (recommended).

