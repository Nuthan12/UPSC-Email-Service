#!/usr/bin/env python3
"""
generate_and_send.py

AI-powered UPSC Daily Brief using Ollama (primary) and optional OpenAI (fallback).
- Ollama endpoint: https://ollama-server-y6ln.onrender.com (user provided)
- Produces Drishti/InsightsonIndia-style structured JSON for each article:
  include, category, section_heading, context, background, key_points, impact, upsc_relevance, source
- Builds a clean PDF with thumbnails and emails it.

Dependencies required in workflow:
pip install feedparser newspaper3k reportlab openai requests pillow readability-lxml
"""

import os, re, time, json, ssl, smtplib, io, datetime, requests
from email.message import EmailMessage
from urllib.parse import urlparse

# ---------------- CONFIG ----------------
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "https://ollama-server-y6ln.onrender.com")
OLLAMA_API = OLLAMA_BASE.rstrip("/") + "/api/generate"

# Curated UPSC-centric feeds (you can add more)
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
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")  # default model name on your Ollama server
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # if you use OpenAI

# ---------------- HELPERS ----------------
def clean_extracted_text(raw_text):
    """Remove nav/footer noise and very short lines. Return cleaned paragraphs."""
    if not raw_text:
        return ""
    text = raw_text.replace("\r", "\n")
    junk_patterns = [r"SEE ALL NEWSLETTERS", r"e-?Paper", r"ADVERTISEMENT", r"LOGIN", r"Subscribe",
                     r"Related Stories", r"Continue reading", r"Read more", r"Click here"]
    for p in junk_patterns:
        text = re.sub(p, " ", text, flags=re.I)
    # remove very short lines / menus
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def extract_article_text_and_image(url, timeout=12):
    """
    Try newspaper3k, then readability, then a basic fallback.
    Returns (clean_text, image_bytes_or_None).
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    # newspaper3k
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

    # readability-lxml fallback
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

    # basic fallback: strip tags
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\\1>)', ' ', r.text)
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
    """Extract first {...} JSON substring and return dict or None."""
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

# ---------------- Ollama summarization (primary) ----------------
def ollama_summarize(title, text, url, model=OLLAMA_MODEL, timeout=120):
    """
    Call Ollama server /api/generate with a prompt that asks for strict JSON.
    Expect a JSON-like reply in the response field. Returns dict or None.
    """
    prompt = f"""
You are an editor preparing concise Drishti/InsightsonIndia-style current affairs notes for UPSC.
Given the article title, URL, and article text, decide if it should be included for UPSC preparation (include: "yes"/"no").
If include = "yes", produce EXACT JSON ONLY with keys:
include, category, section_heading, context, background, key_points, impact, upsc_relevance, source

- category: one of [GS1, GS2, GS3, GS4, FFP, CME, Mapping, Misc]
- section_heading: short heading <=10 words
- context: 1-2 sentences
- background: 2-3 concise sentences
- key_points: array/list of 3-5 short bullets (<=25 words each)
- impact: 1 sentence
- upsc_relevance: 1 short sentence (which GS paper and why)
- source: the article URL

Article title: {title}
Article URL: {url}
Article text (trimmed): {text[:3500]}

Output strict JSON only.
"""
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": 800,
        # Ollama APIs may vary; many accept "temperature" / "top_p" etc.
        "temperature": 0.0
    }
    try:
        r = requests.post(OLLAMA_API, json=payload, timeout=timeout)
        if r.status_code != 200:
            print("Ollama returned status", r.status_code, r.text[:400])
            return None
        data = r.json()
        # Ollama may return {"response": "..."} or similar; try common keys
        resp_text = None
        if isinstance(data, dict):
            if "response" in data:
                resp_text = data["response"]
            elif "result" in data and isinstance(data["result"], dict) and "content" in data["result"]:
                resp_text = data["result"]["content"]
            else:
                # try to pick first string value
                for v in data.values():
                    if isinstance(v, str) and "{" in v:
                        resp_text = v
                        break
        if not resp_text:
            # If response isn't in JSON, try text
            resp_text = r.text
        parsed = extract_json_substring(resp_text)
        return parsed
    except Exception as e:
        print("Ollama summarization failed:", e)
        return None

# ---------------- OpenAI summarization (optional) ----------------
def openai_summarize(openai_key, title, text, url):
    """Call OpenAI (new client) to produce the same strict JSON. Returns dict or None."""
    try:
        from openai import OpenAI
    except Exception as e:
        print("OpenAI client import failed:", e)
        return None
    client = OpenAI(api_key=openai_key)
    prompt = f"""
You are an editor preparing concise Drishti/InsightsonIndia-style current affairs notes for UPSC.
Given the article title, URL and article text, produce ONLY a JSON object with keys:
include, category, section_heading, context, background, key_points, impact, upsc_relevance, source

Article Title: {title}
Article URL: {url}
Article text (trimmed): {text[:3500]}

Keep fields concise. Output valid JSON only.
"""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            temperature=0.0
        )
        raw = resp.choices[0].message["content"]
        parsed = extract_json_substring(raw)
        if parsed:
            return parsed
        # try direct parse
        try:
            return json.loads(raw)
        except Exception:
            return None
    except Exception as ex:
        print("OpenAI API call failed:", ex)
        return None

# ---------------- PDF generation ----------------
def build_pdf(structured_items, pdf_path):
    # imports here so script can run even if reportlab missing until workflow installs it
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

    content.append(Paragraph("Note: Summaries auto-generated. Verify facts from original source and official releases (PIB/The Hindu)", styles['Meta']))
    doc.build(content)

# ---------------- Email ----------------
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
    openai_key = os.environ.get("OPENAI_API_KEY")
    # gather candidates
    entries = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                entries.append({"title": e.get("title",""), "link": e.get("link","")})
        except Exception as ex:
            print("Feed error", feed, ex)

    # dedupe and limit
    seen = set(); candidates = []
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
        # 1) Try OpenAI if key provided (preferred), fallback to Ollama
        if openai_key:
            parsed = openai_summarize(openai_key, title, text, link)
            if parsed is None:
                print("OpenAI failed or returned no valid parse; falling back to Ollama.")
        if parsed is None:
            parsed = ollama_summarize(title, text, link)

        if not parsed:
            print(" -> no parsed JSON from models; skipping.")
            continue

        # ensure include
        inc = str(parsed.get("include","no")).lower()
        if inc != "yes":
            print(" -> model marked not relevant; skipping.")
            continue

        # Attach image bytes and normalize fields
        parsed["image_bytes"] = img_bytes
        parsed.setdefault("key_points", [])
        parsed.setdefault("background", "")
        parsed.setdefault("impact", "")
        parsed.setdefault("upsc_relevance", "")
        parsed.setdefault("source", link)
        # normalize key_points from string to list if needed
        kp = parsed.get("key_points", [])
        if isinstance(kp, str):
            # split heuristically on line breaks or semicolons or full stops
            kp_list = [s.strip() for s in re.split(r'[\\r\\n;•\\-]+', kp) if s.strip()]
            parsed["key_points"] = kp_list[:5]
        structured.append(parsed)
        included += 1
        print(f" -> included (category: {parsed.get('category')})")
        time.sleep(1.0)  # polite pause

    if not structured:
        print("No UPSC-relevant items found today. (You can relax filter or add more UPSC feeds.)")
        # optional fallback behaviour: uncomment to force delivery of top headlines
        # for c in candidates[:3]:
        #     structured.append({
        #         "category":"Misc", "section_heading":c['title'], "context":"Auto-added headline for testing.",
        #         "background":"", "key_points":[c['title']], "impact":"", "upsc_relevance":"",
        #         "source":c['link'], "image_bytes":None})
        # if not structured: return

    # build PDF & email
    pdf_name = f"UPSC_AI_Brief_{datetime.date.today().isoformat()}.pdf"
    build_pdf(structured, pdf_name)
    print("PDF created:", pdf_name)
    email_pdf(pdf_name)

if __name__ == "__main__":
    main()