#!/usr/bin/env python3
"""
generate_and_send.py

AI-powered UPSC Daily Brief using Groq API (primary), optional OpenAI fallback,
and an offline fallback to ensure you always receive a PDF.

Prereqs (Actions / environment):
- Secrets: GROQ_API_KEY, SMTP_USER, SMTP_PASSWORD, EMAIL_TO
- Optional: OPENAI_API_KEY (if you want OpenAI as a second-tier fallback)
- Dependencies: feedparser, newspaper3k, reportlab, requests, pillow, readability-lxml
  (Install in workflow: pip install feedparser newspaper3k reportlab requests pillow readability-lxml openai)
"""

import os
import re
import time
import json
import ssl
import smtplib
import io
import datetime
import requests
from email.message import EmailMessage
from urllib.parse import urlparse

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"  # Groq-compatible OpenAI path
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # optional
GROQ_MODEL = os.environ.get("GROQ_MODEL", "mixtral-8x7b")     # change to "llama3-8b" if preferred

# Curated UPSC-centric feeds
RSS_FEEDS = [
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://prsindia.org/theprsblog/feed",
    "https://www.thehindu.com/news/national/feeder/default.rss",
    "https://www.thehindu.com/opinion/lead/feeder/default.rss",
    "https://www.drishtiias.com/feed",
    "https://www.insightsonindia.com/feed",
    "https://www.downtoearth.org.in/rss/all.xml",
]

MAX_CANDIDATES = 25
MAX_INCLUSIONS = 12

# ---------------- UTILITIES ----------------
def clean_extracted_text(raw_text):
    if not raw_text:
        return ""
    text = raw_text.replace("\r", "\n")
    junk_patterns = [r"SEE ALL NEWSLETTERS", r"e-?Paper", r"ADVERTISEMENT", r"LOGIN", r"Subscribe",
                     r"Related Stories", r"Continue reading", r"Read more", r"Click here"]
    for p in junk_patterns:
        text = re.sub(p, " ", text, flags=re.I)
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def extract_article_text_and_image(url, timeout=12):
    headers = {"User-Agent": "Mozilla/5.0"}
    # newspaper3k attempt
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        text = clean_extracted_text(art.text)
        top_image = getattr(art, "top_image", None)
        img_bytes = None
        if top_image:
            try:
                r = requests.get(top_image, timeout=timeout, headers=headers)
                if r.status_code == 200 and 'image' in r.headers.get('Content-Type', ''):
                    img_bytes = r.content
            except Exception:
                img_bytes = None
        if text and len(text.split()) > 60:
            return text, img_bytes
    except Exception:
        pass

    # readability fallback
    try:
        from readability import Document
        r = requests.get(url, timeout=timeout, headers=headers)
        doc = Document(r.text)
        summary_html = doc.summary()
        text = re.sub(r'<[^>]+>', ' ', summary_html)
        text = clean_extracted_text(text)
        img_bytes = None
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code == 200 and 'image' in r2.headers.get('Content-Type', ''):
                    img_bytes = r2.content
            except Exception:
                img_bytes = None
        if text and len(text.split()) > 60:
            return text, img_bytes
    except Exception:
        pass

    # basic fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', ' ', r.text)
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = ' '.join(stripped.split())
        stripped = clean_extracted_text(stripped)
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code == 200 and 'image' in r2.headers.get('Content-Type', ''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if stripped and len(stripped.split()) > 60:
            return stripped, img_bytes
    except Exception:
        pass

    return "", None

def extract_json_substring(s):
    if not s:
        return None
    start = s.find('{')
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i+1])
                except Exception:
                    return None
    return None

# ---------------- GROQ (primary) ----------------
def call_with_retries(url, headers, payload, attempts=3, backoff=3, timeout=90):
    for i in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            print(f"[groq] attempt {i+1} status {r.status_code}")
            # print short body snippet for debugging
            body_snippet = r.text[:800].replace("\n", " ")
            print("[groq] body_snippet:", body_snippet)
            return r
        except Exception as ex:
            print(f"[groq] exception on attempt {i+1}:", ex)
            time.sleep(backoff * (i+1))
    return None

def groq_summarize(title, text, url, timeout=90):
    if not GROQ_API_KEY:
        print("No GROQ_API_KEY found.")
        return None
    prompt = f"""
You are a UPSC current-affairs editor. Summarize this article in Drishti/InsightsIAS style.
Output valid JSON only with keys:
include, category, section_heading, context, background, key_points, impact, upsc_relevance, source

Article title: {title}
Article URL: {url}
Article text (trimmed): {text[:3500]}
"""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 900
    }
    r = call_with_retries(GROQ_ENDPOINT, headers, payload, attempts=3, backoff=3, timeout=timeout)
    if not r:
        print("Groq: no response after retries.")
        return None
    if r.status_code != 200:
        print("Groq returned non-200:", r.status_code)
        return None
    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        parsed = extract_json_substring(content)
        return parsed
    except Exception as ex:
        print("Groq parse error:", ex)
        return None

# ---------------- OpenAI fallback (optional) ----------------
def openai_summarize(openai_key, title, text, url):
    if not openai_key:
        return None
    try:
        from openai import OpenAI
    except Exception as e:
        print("OpenAI client import failed:", e)
        return None
    client = OpenAI(api_key=openai_key)
    prompt = f"""
You are a UPSC editor. Return strict JSON only with keys:
include, category, section_heading, context, background, key_points, impact, upsc_relevance, source

Title: {title}
URL: {url}
Text (trimmed): {text[:3500]}
"""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}],
            max_tokens=700,
            temperature=0.0
        )
        raw = resp.choices[0].message["content"]
        parsed = extract_json_substring(raw)
        return parsed
    except Exception as ex:
        print("OpenAI error:", ex)
        return None

# ---------------- Offline fallback ----------------
def offline_summary_fallback(title, text, url):
    sents = [s.strip() for s in re.split(r'\.|\n', text) if len(s.strip())>40]
    context = sents[0] if sents else title
    background = " ".join(sents[1:3]) if len(sents)>1 else ""
    key_points = sents[1:5] if len(sents)>1 else [title]
    return {
        "include":"yes",
        "category":"Misc",
        "section_heading": title[:100],
        "context": context[:600],
        "background": background[:800],
        "key_points": key_points[:5],
        "impact": "",
        "upsc_relevance": "General current affairs",
        "source": url,
        "image_bytes": None
    }

# ---------------- PDF generation ----------------
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
    styles.add(ParagraphStyle(name='Meta', fontSize=8, leading=10, textColor=colors.grey))

    content = []
    today = datetime.datetime.now().strftime("%d %B %Y")
    header = Table([[Paragraph(f'UPSC CURRENT AFFAIRS — {today}', styles['TitleLarge'])]], colWidths=(doc.width,))
    header.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1), colors.HexColor("#f4f8fb")),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    content.append(header)
    content.append(Spacer(1,8))

    order = ['GS2','GS3','GS1','GS4','CME','FFP','Mapping','Misc']
    for cat in order:
        items = [it for it in structured_items if it.get('category')==cat]
        if not items:
            continue
        content.append(Paragraph(cat, styles['Section']))
        for it in items:
            content.append(Paragraph(f"<b>{it.get('section_heading','')}</b>", styles['Body']))
            imgb = it.get('image_bytes')
            if imgb:
                try:
                    img = PILImage.open(io.BytesIO(imgb))
                    img.thumbnail((220,120))
                    bb = io.BytesIO()
                    img.save(bb, format='PNG')
                    bb.seek(0)
                    content.append(Image(bb, width=150, height=80))
                except Exception:
                    pass
            if it.get('context'):
                content.append(Paragraph(f"<i>Context:</i> {it.get('context')}", styles['Body']))
            if it.get('background'):
                content.append(Paragraph(f"<b>Background:</b> {it.get('background')}", styles['Body']))
            for kp in it.get('key_points', []):
                content.append(Paragraph(f"• {kp}", styles['Body']))
            if it.get('impact'):
                content.append(Paragraph(f"<b>Impact/Significance:</b> {it.get('impact')}", styles['Body']))
            if it.get('upsc_relevance'):
                content.append(Paragraph(f"<b>UPSC Relevance:</b> {it.get('upsc_relevance')}", styles['Body']))
            if it.get('source'):
                content.append(Paragraph(f"Source: {it.get('source')}", styles['Meta']))
            content.append(Spacer(1,10))
        content.append(Spacer(1,6))

    content.append(Paragraph("Note: Summaries auto-generated. Verify facts from original source and official releases (PIB/The Hindu).", styles['Meta']))
    doc.build(content)

# ---------------- EMAIL ----------------
def email_pdf(pdf_path):
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASS = os.environ.get("SMTP_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO")
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))

    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        raise EnvironmentError("Missing SMTP_USER / SMTP_PASSWORD / EMAIL_TO secrets")

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"UPSC Daily Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg.set_content("Attached: UPSC Daily Current Affairs — AI generated (Drishti-style).")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------------- MAIN FLOW ----------------
def main():
    import feedparser
    openai_key = OPENAI_API_KEY
    entries = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                entries.append({"title": e.get("title",""), "link": e.get("link","")})
        except Exception as ex:
            print("Feed error", feed, ex)

    seen = set()
    candidates = []
    for e in entries:
        if e["link"] in seen: continue
        seen.add(e["link"])
        candidates.append(e)
        if len(candidates) >= MAX_CANDIDATES: break

    structured = []
    included = 0

    for e in candidates:
        if included >= MAX_INCLUSIONS: break
        title = e.get("title","")
        link = e.get("link","")
        print("Processing:", title)
        text, img_bytes = extract_article_text_and_image(link)
        if not text:
            print(" -> no extractable text, skipping")
            continue

        parsed = None
        # 1) Try Groq first
        parsed = groq_summarize(title, text, link)
        if parsed is None and openai_key:
            print("Groq failed or returned nothing; trying OpenAI fallback.")
            parsed = openai_summarize(openai_key, title, text, link)

        if parsed is None:
            print("Both Groq/OpenAI failed; using offline fallback for this article.")
            parsed = offline_summary_fallback(title, text, link)

        if not parsed:
            print(" -> no parsed JSON available, skipping")
            continue

        # ensure include is present (offline fallback sets include=yes)
        if str(parsed.get("include","yes")).lower() != "yes":
            print(" -> model marked not relevant; skipping.")
            continue

        # attach image bytes (may be None)
        parsed["image_bytes"] = img_bytes
        parsed.setdefault("key_points", [])
        parsed.setdefault("background", "")
        parsed.setdefault("impact", "")
        parsed.setdefault("upsc_relevance", "")
        parsed.setdefault("source", link)

        # normalize key_points to list
        kp = parsed.get("key_points", [])
        if isinstance(kp, str):
            kp_list = [s.strip() for s in re.split(r'[\r\n;•\-]+', kp) if s.strip()]
            parsed["key_points"] = kp_list[:5]

        structured.append(parsed)
        included += 1
        print(f" -> included (category: {parsed.get('category')})")
        time.sleep(1.0)

    if not structured:
        print("No UPSC-relevant items found; generating a minimal fallback PDF.")
        # minimal fallback: take top 3 headlines
        for c in candidates[:3]:
            structured.append({
                "category":"Misc",
                "section_heading": c.get("title",""),
                "context": "Auto-added headline — no AI summary available today.",
                "background": "",
                "key_points": [c.get("title","")],
                "impact": "",
                "upsc_relevance": "",
                "source": c.get("link",""),
                "image_bytes": None
            })

    pdf_name = f"UPSC_AI_Brief_{datetime.date.today().isoformat()}.pdf"
    build_pdf(structured, pdf_name)
    print("PDF created:", pdf_name)
    try:
        email_pdf(pdf_name)
    except Exception as ex:
        print("Email sending failed:", ex)

if __name__ == "__main__":
    main()