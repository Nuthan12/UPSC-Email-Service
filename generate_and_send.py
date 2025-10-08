#!/usr/bin/env python3
"""
generate_and_send.py — AI-powered UPSC Daily Brief
---------------------------------------------------
Features:
- Fetches news from multiple UPSC-relevant RSS feeds
- Cleans out junk HTML text (no "SEE ALL NEWSLETTERS" etc.)
- Extracts main text + top image (newspaper3k/readability)
- Summarizes & classifies with OpenAI (GS1–GS4, FFP, CME, etc.)
- Builds a colored PDF with section headers & article images
- Emails the PDF daily at 8:30 AM IST (via GitHub Actions)

Required secrets:
- OPENAI_API_KEY
- SMTP_USER
- SMTP_PASSWORD
- EMAIL_TO
"""

import os, re, ssl, smtplib, io, datetime, time, json, requests
from email.message import EmailMessage
from urllib.parse import urlparse

# ---------------- CONFIG ----------------
RSS_FEEDS = [
    "https://www.thehindu.com/news/feeder/default.rss",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://www.reuters.com/world/rss.xml",
    "https://www.thehindu.com/opinion/lead/feeder/default.rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ndtv.com/rss/india.xml",
]

MAX_ARTICLES = 15
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# ---------------- CLEANERS ----------------
def clean_extracted_text(raw_text):
    """Remove navigation, footer, and junk content from HTML text."""
    if not raw_text:
        return ""
    text = raw_text.replace("\r", "\n")
    junk_patterns = [
        r"SEE ALL NEWSLETTERS", r"e-?Paper", r"LOGIN", r"Subscribe",
        r"ADVERTISEMENT", r"Related Stories", r"Continue reading",
        r"Read more", r"Click here", r"Skip links", r"FRONT PAGE",
        r"Search", r"Account", r"Top Stories"
    ]
    for pat in junk_patterns:
        text = re.sub(pat, " ", text, flags=re.I)

    lines = []
    for line in text.splitlines():
        s = line.strip()
        if len(s) < 40:
            continue
        if re.match(r"^[A-Z\s]{15,}$", s):
            continue
        lines.append(s)
    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

# ---------------- EXTRACTION ----------------
def extract_article_text_and_image(url, timeout=12):
    """Extract main text and top image using newspaper3k/readability."""
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        text = clean_extracted_text(art.text)
        top_image = art.top_image if hasattr(art, "top_image") else None
        img_bytes, img_ext = None, None
        if top_image:
            try:
                r = requests.get(top_image, timeout=timeout)
                if "image" in r.headers.get("Content-Type", ""):
                    img_bytes = r.content
                    img_ext = top_image.split(".")[-1]
            except:
                pass
        if text and len(text.split()) > 40:
            return text, img_bytes, img_ext
    except Exception:
        pass

    # fallback: readability
    try:
        from readability import Document
        r = requests.get(url, timeout=timeout)
        doc = Document(r.text)
        summary_html = doc.summary()
        text = re.sub(r"<[^>]+>", " ", summary_html)
        text = clean_extracted_text(text)
        img_bytes, img_ext = None, None
        m = re.search(r'og:image" content="([^"]+)"', r.text)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout)
                if "image" in r2.headers.get("Content-Type", ""):
                    img_bytes = r2.content
                    img_ext = "jpg"
            except:
                pass
        if text and len(text.split()) > 40:
            return text, img_bytes, img_ext
    except Exception:
        pass

    return "", None, None

# ---------------- OPENAI SUMMARIZATION ----------------
def openai_summarize(openai_key, title, text):
    """Ask OpenAI to classify and summarize in UPSC-style."""
    import openai
    openai.api_key = openai_key
    prompt = f"""
You are an assistant creating UPSC-style current affairs notes.
Summarize and classify this article into one of:
[GS1, GS2, GS3, GS4, FFP, CME, Mapping, Misc].
Provide output JSON with keys:
category, section_heading, context, key_points (list), significance, prelim_fact.

Article title: {title}
Article text (trimmed): {text[:3000]}
"""
    try:
        resp = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI summarization failed:", e)
        return None

# ---------------- PDF GENERATION ----------------
def build_pdf(structured_items, pdf_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from PIL import Image as PILImage
    import io, datetime

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleLarge', fontSize=16, alignment=1, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name='Section', fontSize=12, leading=14, spaceAfter=4, spaceBefore=8, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name='Body', fontSize=10, leading=13))
    styles.add(ParagraphStyle(name='Small', fontSize=8, leading=10, textColor=colors.grey))

    content = []
    today = datetime.datetime.now().strftime("%d %B %Y")
    header_table = Table([[Paragraph(f"UPSC CURRENT AFFAIRS – {today}", styles["TitleLarge"])]],
                         colWidths=(doc.width,))
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f8fb")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6)
    ]))
    content.append(header_table)
    content.append(Spacer(1, 10))

    order = ["GS2", "GS3", "GS1", "GS4", "CME", "FFP", "Mapping", "Misc"]
    for cat in order:
        items = [i for i in structured_items if i.get("category") == cat]
        if not items:
            continue
        content.append(Paragraph(cat, styles["Section"]))
        for it in items:
            content.append(Paragraph(f"<b>{it['section_heading']}</b>", styles["Body"]))
            if it.get("image_bytes"):
                try:
                    img = PILImage.open(io.BytesIO(it["image_bytes"]))
                    img.thumbnail((160, 100))
                    bio = io.BytesIO()
                    img.save(bio, format="PNG")
                    bio.seek(0)
                    content.append(Image(bio, width=80, height=50))
                except Exception:
                    pass
            if it.get("context"):
                content.append(Paragraph(f"<i>Context:</i> {it['context']}", styles["Body"]))
            for point in it.get("key_points", []):
                content.append(Paragraph(f"• {point}", styles["Body"]))
            if it.get("significance"):
                content.append(Paragraph(f"<b>Significance:</b> {it['significance']}", styles["Body"]))
            if it.get("prelim_fact"):
                content.append(Paragraph(f"<b>Fact for Prelims:</b> {it['prelim_fact']}", styles["Body"]))
            content.append(Spacer(1, 8))
    content.append(Paragraph("Note: Auto-generated summaries. Verify key data from official sources (PIB, The Hindu).", styles["Small"]))
    doc.build(content)

# ---------------- EMAIL ----------------
def email_pdf(pdf_path):
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASS = os.environ.get("SMTP_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO")
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"UPSC Daily Brief – {datetime.datetime.now().strftime('%d %b %Y')}"
    msg.set_content("Attached is your AI-powered UPSC Current Affairs Brief.")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=context)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"✅ Email sent to {EMAIL_TO}")

# ---------------- MAIN ----------------
def main():
    import feedparser
    entries = []
    for feed in RSS_FEEDS:
        d = feedparser.parse(feed)
        for e in d.entries:
            entries.append({"title": e.title, "link": e.link})
    entries = entries[:MAX_ARTICLES]
    openai_key = os.environ.get("OPENAI_API_KEY")
    structured = []

    for e in entries:
        print("Processing:", e["title"])
        text, img_bytes, img_ext = extract_article_text_and_image(e["link"])
        if not text:
            continue
        json_out = openai_summarize(openai_key, e["title"], text)
        try:
            parsed = json.loads(json_out)
        except:
            parsed = {"category": "Misc", "section_heading": e["title"], "context": text[:200], "key_points": [], "significance": "", "prelim_fact": ""}
        kp = parsed.get("key_points", [])
        if isinstance(kp, str):
            kp = [x.strip() for x in kp.split(".") if x.strip()]
        parsed["key_points"] = kp
        parsed["image_bytes"] = img_bytes
        structured.append(parsed)
        time.sleep(1)

    pdf_path = f"UPSC_AI_Brief_{datetime.date.today()}.pdf"
    build_pdf(structured, pdf_path)
    email_pdf(pdf_path)

if __name__ == "__main__":
    main()