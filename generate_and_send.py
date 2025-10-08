#!/usr/bin/env python3
"""
generate_and_send.py — AI-powered UPSC Daily Brief (Drishti/Insights style)

Behaviour improvements:
- Uses OpenAI to (A) check UPSC relevance, (B) produce Drishti-style structured writeups in JSON.
- Filters out non-relevant headlines (only includes items where model says include: "yes").
- PDF sections: Title, Context, Background, Key Points, Impact/Significance, UPSC Relevance, Source.
- Keeps image thumbnails where available.
- Intended to be run daily by GitHub Actions (08:30 IST).
Required repo secrets:
- OPENAI_API_KEY
- SMTP_USER, SMTP_PASSWORD, EMAIL_TO
"""

import os, re, time, json, ssl, smtplib, io, datetime, requests
from email.message import EmailMessage
from urllib.parse import urlparse

# ---------- CONFIG ----------
RSS_FEEDS = [
    "https://www.thehindu.com/news/feeder/default.rss",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://www.reuters.com/world/rss.xml",
    "https://www.thehindu.com/opinion/lead/feeder/default.rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ndtv.com/rss/india.xml",
]
MAX_CANDIDATES = 25         # number of feed items to consider
MAX_INCLUSIONS = 12         # max articles to include in PDF
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # change if needed

# ---------- UTILITIES ----------
def clean_extracted_text(raw_text):
    if not raw_text:
        return ""
    # minimal cleaning to remove large nav/footer noise
    text = raw_text.replace("\r", "\n")
    text = re.sub(r'\n{2,}', '\n\n', text)
    junk_patterns = [r'SEE ALL NEWSLETTERS', r'e-?Paper', r'ADVERTISEMENT', r'LOGIN', r'Subscribe', r'Related Stories', r'Continue reading']
    for p in junk_patterns:
        text = re.sub(p, ' ', text, flags=re.I)
    # remove extremely short lines
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip())>30]
    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def extract_article_text_and_image(url, timeout=12):
    """Try newspaper3k, then readability, then basic fallback. Return (text, image_bytes)."""
    # newspaper
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        txt = clean_extracted_text(art.text)
        top_image = getattr(art, "top_image", None)
        img_bytes = None
        if top_image:
            try:
                r = requests.get(top_image, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
                if r.status_code==200 and "image" in r.headers.get("Content-Type",""):
                    img_bytes = r.content
            except Exception:
                img_bytes = None
        if txt and len(txt.split())>60:
            return txt, img_bytes
    except Exception:
        pass

    # readability fallback
    try:
        from readability import Document
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        doc = Document(r.text)
        summary_html = doc.summary()
        text = re.sub(r'<[^>]+>', ' ', summary_html)
        text = clean_extracted_text(text)
        # try og:image
        img_bytes = None
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout)
                if r2.status_code==200 and "image" in r2.headers.get("Content-Type",""):
                    img_bytes = r2.content
            except Exception:
                img_bytes = None
        if text and len(text.split())>60:
            return text, img_bytes
    except Exception:
        pass

    # final basic fallback: strip tags & return first chunk
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\\1>)', ' ', r.text)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = ' '.join(text.split())
        text = clean_extracted_text(text)
        # og:image if present
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout)
                if r2.status_code==200 and "image" in r2.headers.get("Content-Type",""):
                    img_bytes = r2.content
            except:
                pass
        if text and len(text.split())>60:
            return text, img_bytes
    except Exception:
        pass

    return "", None

# ---------- OPENAI: relevance + Drishti-style writeup ----------
def ask_openai_for_structured(openai_key, title, text, url):
    """Ask OpenAI to decide relevance and return a strict JSON in Drishti style.
    JSON fields:
      include: "yes" or "no"
      category: GS1/GS2/GS3/GS4/FFP/CME/Mapping/Misc
      section_heading: short heading
      context: 1-2 sentences
      background: 2-3 sentences (concise)
      key_points: list of 3-5 short bullets
      impact: 1 sentence
      upsc_relevance: 1 short sentence
      source: original source/link
    """
    import openai
    openai.api_key = openai_key
    prompt = f"""
You are an editor that prepares concise Drishti/InsightsonIndia-style current affairs writeups for UPSC exam preparation.
Given an article title, URL and article text, do two things:
1) Decide if this article is relevant for UPSC syllabus (include only if yes).
2) If relevant, produce a JSON object (ONLY JSON, no extra commentary) with exact keys:
   include, category, section_heading, context, background, key_points, impact, upsc_relevance, source
- 'include' must be "yes" or "no".
- 'category' must be one of: GS1, GS2, GS3, GS4, FFP, CME, Mapping, Misc.
- 'section_heading' should be a short headline (<=10 words).
- 'context' 1-2 sentences summary of what happened.
- 'background' 2-3 short sentences of necessary background.
- 'key_points' must be a JSON array of 3-5 short bullets (each <= 25 words).
- 'impact' 1 sentence on broader significance.
- 'upsc_relevance' 1 sentence telling how it ties to the syllabus (which GS paper/section).
- 'source' should include the original URL.

Article Title: {title}
Article URL: {url}
Article text (first 3500 chars): {text[:3500]}

Be strict: only output valid JSON. Keep language exam-focused, neutral, and concise.
"""
    try:
        resp = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user", "content": prompt}],
            max_tokens=450,
            temperature=0.0,
        )
        out = resp.choices[0].message.content.strip()
        # Some models may wrap the JSON in triple backticks — extract JSON substring
        j = extract_json_substring(out)
        return j
    except Exception as e:
        print("OpenAI error:", e)
        return None

def extract_json_substring(s):
    """Extract first {...} JSON substring from a string; return as Python dict or None."""
    s = s.strip()
    # find first { and matching }
    start = s.find("{")
    if start == -1:
        return None
    # naive bracket matching
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    json_text = s[start:i+1]
                    return json.loads(json_text)
                except Exception:
                    return None
    return None

# ---------- PDF generation (clean layout with colors) ----------
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

    # order preference
    order = ['GS2','GS3','GS1','GS4','CME','FFP','Mapping','Misc']
    for cat in order:
        items = [it for it in structured_items if it.get('category')==cat]
        if not items:
            continue
        content.append(Paragraph(cat, styles['Section']))
        for it in items:
            content.append(Paragraph(f"<b>{it.get('section_heading','')}</b>", styles['Body']))
            # image
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

    content.append(Paragraph("Note: Summaries auto-generated. Verify facts from original sources and PIB for official numbers.", styles['Meta']))
    doc.build(content)

# ---------- Email ----------
def email_pdf(pdf_path):
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASS = os.environ.get("SMTP_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO")
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))

    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        raise EnvironmentError("Missing SMTP_USER / SMTP_PASSWORD / EMAIL_TO secrets")

    msg = EmailMessage()
    msg['From'] = SMTP_USER
    msg['To'] = EMAIL_TO
    msg['Subject'] = f"UPSC Daily Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg.set_content("Attached: UPSC Daily Current Affairs — AI generated (Drishti-style).")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------- MAIN FLOW ----------
def main():
    import feedparser
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("WARNING: OPENAI_API_KEY not set. Script will run with conservative fallback (less accurate).")

    # gather candidate entries
    entries = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                entries.append({"title": e.title, "link": e.link, "published": getattr(e, 'published', '')})
        except Exception as ex:
            print("Feed error", feed, ex)
    # dedupe preserve order
    seen = set(); candidates = []
    for e in entries:
        if e['link'] in seen: continue
        seen.add(e['link']); candidates.append(e)
        if len(candidates) >= MAX_CANDIDATES: break

    structured = []
    included = 0
    for e in candidates:
        if included >= MAX_INCLUSIONS: break
        print("Processing:", e['title'])
        text, img_bytes = extract_article_text_and_image(e['link'])
        if not text:
            print("  -> no extracted text, skipping.")
            continue

        parsed = None
        if openai_key:
            j = ask_openai_for_structured(openai_key, e['title'], text, e['link'])
            if j and isinstance(j, dict) and j.get('include','no').lower()=='yes':
                # attach image bytes into parsed dict
                j['image_bytes'] = img_bytes
                # ensure keys exist
                j.setdefault('key_points', [])
                j.setdefault('background', '')
                j.setdefault('impact', '')
                j.setdefault('upsc_relevance', '')
                j.setdefault('source', e['link'])
                parsed = j
        else:
            # lightweight heuristic fallback:
            txt_lower = text.lower()
            keywords = ['government', 'cabinet', 'supreme court', 'bill', 'act', 'policy', 'agreement', 'india', 'defence', 'rbi', 'msm', 'msps', 'nobel', 'award']
            if any(k in txt_lower for k in keywords):
                parsed = {
                    'include': 'yes',
                    'category': 'Misc',
                    'section_heading': e['title'][:120],
                    'context': text.split('\n\n')[0][:300],
                    'background': '',
                    'key_points': [s.strip() for s in text.split('.')[:3] if len(s.strip())>20],
                    'impact': '',
                    'upsc_relevance': 'General current affairs',
                    'source': e['link'],
                    'image_bytes': img_bytes
                }

        if parsed:
            structured.append(parsed)
            included += 1
            print("  -> included (category:", parsed.get('category'), ")")
        else:
            print("  -> skipped (not UPSC-relevant).")
        time.sleep(1.0)

    if not structured:
        print("No UPSC-relevant items found today.")
        return

    pdf_name = f"UPSC_AI_Brief_{datetime.date.today().isoformat()}.pdf"
    build_pdf(structured, pdf_name)
    print("PDF created:", pdf_name)
    email_pdf(pdf_name)

if __name__ == "__main__":
    main()