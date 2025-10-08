#!/usr/bin/env python3
"""
generate_and_send.py

Grouped UPSC Daily Brief generator
- Shows UPSC relevance at section header only
- Removes per-card UPSC relevance and source display
- Keeps robust extraction, safe_trim, Groq/OpenAI/offline fallback,
  safe image handling, chunked PDF layout, logo, email
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

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "mixtral-8x7b")

# UPSC focused feeds
RSS_FEEDS = [
    "https://www.drishtiias.com/feed",
    "https://www.insightsonindia.com/feed",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://prsindia.org/theprsblog/feed",
    "https://www.thehindu.com/news/national/feeder/default.rss",
    "https://www.downtoearth.org.in/rss/all.xml",
]

MAX_CANDIDATES = 30
MAX_INCLUSIONS = 12

UPSC_KEYWORDS = [
    "upsc", "governance", "policy", "environment", "economy",
    "ethics", "international", "constitution", "biodiversity",
    "reform", "initiative", "scheme", "pib", "report", "summit",
    "nobel", "isro", "climate", "vaccine", "trade", "rbi", "supreme court",
    "study", "research", "index", "survey", "mapping", "geography", "map"
]

CATEGORY_KEYWORDS = {
    "GS1": ["tribal", "caste", "society", "population", "culture", "heritage",
            "migration", "demography", "education", "health", "poverty", "geography", "river", "plateau"],
    "GS2": ["constitution", "cabinet", "parliament", "legislation", "judgment",
            "supreme court", "high court", "governance", "police", "bureaucracy",
            "policy", "pib", "court", "minister", "administration"],
    "GS3": ["economy", "gdp", "inflation", "rbi", "bank", "finance", "industry",
            "agriculture", "infrastructure", "climate", "environment", "isro",
            "science", "technology", "nobel", "biodiversity", "ecosystem", "research",
            "energy", "trade", "tectonic", "rift", "sediment", "paleo", "palaeo"],
    "GS4": ["ethics", "integrity", "corruption", "values", "moral",
            "public service", "code of conduct", "ethical"],
    "CME": ["study", "index", "report", "research", "survey", "analysis", "paper", "finding"],
    "Mapping": ["map", "location", "geography", "boundary", "river", "plateau", "island"],
    "FFP": ["finance", "fiscal", "budget", "public finance", "tax", "expenditure"],
    "Misc": []
}

RELEVANCE_KEYWORDS = {
    "environment": ("GS3 — Environment", ["environment", "climate", "biodiversity", "forest", "species", "pollution", "ecosystem", "western ghats", "ghg"]),
    "economy": ("GS3 — Economy", ["rbi", "inflation", "gdp", "fiscal", "budget", "economy", "exports", "imports", "trade", "tax", "finance"]),
    "polity": ("GS2 — Polity & Governance", ["cabinet", "supreme court", "high court", "constitution", "bill", "act", "parliament", "legislation", "govt", "minister"]),
    "international": ("GS2 — International Relations", ["summit", "g20", "un", "treaty", "agreement", "china", "india", "foreign", "diplomacy"]),
    "science": ("GS3 — Science & Tech", ["nobel", "vaccine", "space", "isro", "research", "scientists", "technology", "study", "tectonic", "rift"]),
    "defence": ("GS3 — Security", ["defence", "army", "navy", "air force", "border", "terror", "military"]),
    "social": ("GS1 — Society", ["tribal", "tribe", "caste", "poverty", "education", "health", "disease", "women", "child"]),
    "ethics": ("GS4 — Ethics", ["ethics", "corruption", "integrity", "ethical", "code of conduct"]),
}

# ---------------- Utilities ----------------
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

def safe_trim(text, max_chars=3500):
    if not text or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    last_period = max(window.rfind('.'), window.rfind('!'), window.rfind('?'))
    if last_period and last_period > int(max_chars * 0.6):
        return window[:last_period+1]
    last_nl = window.rfind('\n')
    if last_nl and last_nl > int(max_chars * 0.5):
        return window[:last_nl].rstrip()
    if window and window[-1].isalnum():
        return re.sub(r'\s+\S*?$', '', window)
    return window

def extract_article_text_and_image(url, timeout=12):
    headers = {"User-Agent": "Mozilla/5.0"}
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

# ---------------- Classification & relevance helpers ----------------
def normalize_model_category(cat_str):
    if not cat_str:
        return ""
    s = cat_str.strip().lower()
    m = re.search(r'gs\s*([1-4])', s)
    if m:
        return f"GS{m.group(1)}"
    if any(w in s for w in ["polity", "governance", "constitution", "parliament", "judgment"]):
        return "GS2"
    if any(w in s for w in ["economy", "finance", "gdp", "rbi", "trade", "industry"]):
        return "GS3"
    if any(w in s for w in ["environment", "climate", "biodiversity", "tectonic", "paleo", "nobel", "isro"]):
        return "GS3"
    if any(w in s for w in ["ethic", "ethics", "integrity", "corruption"]):
        return "GS4"
    if any(w in s for w in ["society", "social", "tribal", "caste", "education", "health"]):
        return "GS1"
    if any(w in s for w in ["cme", "study", "report", "index", "research"]):
        return "CME"
    if any(w in s for w in ["map", "mapping", "geography", "location"]):
        return "Mapping"
    if any(w in s for w in ["ffp", "finance", "fiscal", "budget"]):
        return "FFP"
    return ""

def score_category_by_keywords(title, text):
    combined = (title + " " + text).lower()
    scores = {cat: 0.0 for cat in CATEGORY_KEYWORDS.keys()}
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            cnt = combined.count(kw)
            if cnt:
                scores[cat] += cnt * (1.0 + min(len(kw)/12.0, 1.5))
    sorted_scores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = sorted_scores[0]
    if best_score <= 0:
        return "Misc", scores
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    if second_score >= 0.7 * best_score:
        priority = ["GS2", "GS3", "GS1", "GS4", "CME", "FFP", "Mapping", "Misc"]
        for p in priority:
            if abs(scores.get(p,0) - best_score) < 1e-6 or scores.get(p,0) == best_score:
                return p, scores
        return best_cat, scores
    return best_cat, scores

def infer_upsc_relevance_label_for_category(cat):
    # Map canonical category to human-friendly label to show in section header
    mapping = {
        "GS1": "GS1 — Society / Geography",
        "GS2": "GS2 — Polity & Governance",
        "GS3": "GS3 — Economy / Science / Environment",
        "GS4": "GS4 — Ethics",
        "CME": "CME — Studies & Reports",
        "FFP": "FFP — Public Finance",
        "Mapping": "Mapping — Geography / Location",
        "Misc": "Miscellaneous Current Affairs"
    }
    return mapping.get(cat, "Current Affairs")

# ---------------- GROQ / OpenAI / Offline summarizers ----------------
def call_groq_with_retries(url, headers, payload, attempts=3, backoff=3, timeout=90):
    for i in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            print(f"[groq] attempt {i+1} status {r.status_code}")
            body_snippet = r.text[:800].replace("\n", " ")
            print("[groq] body_snippet:", (body_snippet[:700] + '...') if len(body_snippet)>700 else body_snippet)
            return r
        except Exception as ex:
            print(f"[groq] exception attempt {i+1}:", ex)
            time.sleep(backoff * (i + 1))
    return None

def groq_summarize(title, text, url, timeout=90):
    if not GROQ_API_KEY:
        print("No GROQ_API_KEY found.")
        return None
    trimmed = safe_trim(text, max_chars=3500)
    prompt = f"""
You are an expert UPSC content analyst writing like InsightsIAS.

Generate a structured current affairs brief in this JSON format:
{{
  "include": "yes/no",
  "category": "GS1/GS2/GS3/GS4/CME/Mapping/FFP",
  "section_heading": "",
  "context": "",
  "about": "",
  "sub_sections": [
      {{"heading": "Composition / Key Aspects", "points": []}},
      {{"heading": "Emerging Trends / Analysis", "points": []}},
      {{"heading": "UPSC Linkages", "points": []}}
  ],
  "facts_and_data": [],
  "upsc_relevance": "e.g., GS3 — Health / CME"
}}

Guidelines:
- Follow the exact tone and layout of InsightsIAS (Context → About → Sub-sections with bold titles and bullet points).
- Mention factual data, stats, and policy/program names wherever possible.
- For CME articles, add analytical observations and lessons for Mains.
- If the article is from PIB or DrishtiIAS, include schemes or ministries.
- Use short but information-rich sentences.
- Do NOT invent facts.

Title: {title}
Source URL: {url}
Article Text: {safe_trim(text)}
"""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 900}
    r = call_groq_with_retries(GROQ_ENDPOINT, headers, payload, attempts=3, backoff=3, timeout=timeout)
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

def openai_summarize(openai_key, title, text, url):
    if not openai_key:
        return None
    try:
        from openai import OpenAI
    except Exception as e:
        print("OpenAI import failed:", e)
        return None
    client = OpenAI(api_key=openai_key)
    trimmed = safe_trim(text, max_chars=3500)
    prompt = f"""
You are a UPSC editor. Return strict JSON only with:
include, category, section_heading, context, background, key_points, impact, upsc_relevance, source

Title: {title}
URL: {url}
Text (trimmed): {trimmed}

If text seems truncated, set context to "SOURCE_ONLY" and keep other fields minimal.
"""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            temperature=0.0,
        )
        raw = resp.choices[0].message["content"]
        parsed = extract_json_substring(raw)
        return parsed
    except Exception as ex:
        print("OpenAI error:", ex)
        return None

def offline_summary_fallback(title, text, url):
    sents = [s.strip() for s in re.split(r'\.|\n', text) if len(s.strip()) > 40]
    context = sents[0] if sents else title
    background = " ".join(sents[1:3]) if len(sents) > 1 else ""
    key_points = sents[1:5] if len(sents) > 1 else [title]
    return {
        "include": "yes",
        "category": "Misc",
        "section_heading": title[:100],
        "context": context[:600],
        "background": background[:800],
        "key_points": key_points[:5],
        "impact": "",
        "upsc_relevance": "GS2/GS3 — Current Affairs",
        "source": url,
        "image_bytes": None,
    }

# ---------------- Validation ----------------
def looks_truncated(text):
    if not text:
        return False
    t = text.strip()
    if t.endswith('-') or t.endswith('—'):
        return True
    if re.search(r'\d+$', t):
        return True
    if len(t) > 120 and not re.search(r'[\.!?]$', t):
        return True
    return False

def validate_parsed(parsed):
    if not parsed:
        return False
    include = str(parsed.get("include", "yes")).lower()
    if include != "yes":
        return True
    context = (parsed.get("context") or "").strip()
    if context.upper() == "SOURCE_ONLY":
        return True
    background = (parsed.get("background") or "").strip()
    if looks_truncated(context) or looks_truncated(background):
        return False
    kps = parsed.get("key_points", []) or []
    for kp in kps:
        if looks_truncated(kp):
            return False
    return True

# ---------------- Logo generator & PDF builder & email ----------------
def generate_logo_bytes(text="DailyCAThroughAI", size=(420, 80), bgcolor=(31, 78, 121), fg=(255, 255, 255)):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    img = Image.new("RGB", size, bgcolor)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    except Exception:
        try:
            mask = font.getmask(text)
            text_width, text_height = mask.size
        except Exception:
            text_width, text_height = (200, 28)
    x = (size[0] - text_width) / 2
    y = (size[1] - text_height) / 2
    draw.text((x, y), text, font=font, fill=fg)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio.read()

def build_pdf(structured_items, pdf_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from PIL import Image as PILImage
    import io, datetime

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleLarge', fontSize=16, alignment=1, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name='Section', fontSize=12, leading=14, spaceAfter=6, spaceBefore=10, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name='Body', fontSize=10, leading=13))
    styles.add(ParagraphStyle(name='Meta', fontSize=8, leading=10, textColor=colors.grey))

    content = []
    today_str = datetime.datetime.now().strftime("%d %B %Y")

    # header with logo
    logo_bytes = generate_logo_bytes()
    left = RLImage(io.BytesIO(logo_bytes), width=110, height=28) if logo_bytes else Paragraph("DailyCAThroughAI", styles['Body'])
    right = Paragraph(f"<b>UPSC CURRENT AFFAIRS</b><br/>{today_str}", styles['TitleLarge'])
    header_table = Table([[left, right]], colWidths=[120, doc.width-120])
    header_table.setStyle(TableStyle([
        ('VALIGN',(0,0),(1,0),'MIDDLE'),
        ('BACKGROUND',(0,0),(-1,-1), colors.HexColor("#f4f8fb")),
        ('LEFTPADDING',(0,0),(-1,-1),8),
        ('RIGHTPADDING',(0,0),(-1,-1),8),
        ('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    content.append(header_table)
    content.append(Spacer(1,8))

    # group items by category
    categories_order = ["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    grouped = {c: [it for it in structured_items if it.get('category')==c] for c in categories_order}

    for cat in categories_order:
        items = grouped.get(cat, [])
        if not items:
            continue
        # section header with relevance label
        label = infer_upsc_relevance_label_for_category(cat)
        content.append(Paragraph(label, styles['Section']))

        for it in items:
            # build text only (no per-card relevance, no source)
            paras = []
            paras.append(Paragraph(f"<b>{it.get('section_heading','')}</b>", styles['Body']))
            if it.get('context'):
                if (it.get('context') or "").strip().upper() == "SOURCE_ONLY":
                    paras.append(Paragraph(f"<i>Context:</i> See source (SOURCE_ONLY)", styles['Body']))
                else:
                    paras.append(Paragraph(f"<i>Context:</i> {it.get('context')}", styles['Body']))
            if it.get('background'):
                paras.append(Paragraph(f"<b>Background:</b> {it.get('background')}", styles['Body']))
            for kp in it.get('key_points', []):
                paras.append(Paragraph(f"• {kp}", styles['Body']))
            if it.get('impact'):
                paras.append(Paragraph(f"<b>Impact/Significance:</b> {it.get('impact')}", styles['Body']))
            # NOTE: per-card UPSC relevance and source intentionally omitted as per request

            # chunk paragraphs
            chunk_size = 6
            chunks = [paras[i:i + chunk_size] for i in range(0, len(paras), chunk_size)]

            # prepare image for first chunk (if present)
            imgb = it.get('image_bytes')
            right_col_first = None
            if imgb:
                try:
                    img = PILImage.open(io.BytesIO(imgb))
                    img.load()
                    w,h = img.size
                    if w<=0 or h<=0 or w>20000 or h>20000:
                        raise ValueError("image dimensions suspicious")
                    max_w, max_h = 180, 120
                    if w>max_w or h>max_h:
                        img.thumbnail((max_w, max_h))
                    aspect = (img.height / img.width) if img.width else 1
                    if aspect>6 or aspect<0.05:
                        raise ValueError("unusual aspect ratio")
                    bb = io.BytesIO()
                    img.save(bb, format='PNG')
                    bb.seek(0)
                    img_w, img_h = img.size
                    right_col_first = RLImage(bb, width=min(img_w,150), height=min(img_h,100))
                except Exception as e:
                    print("⚠️ image skipped for article:", e)
                    right_col_first = None

            for idx, chunk in enumerate(chunks):
                try:
                    if idx==0 and right_col_first:
                        tbl = Table([[chunk, right_col_first]], colWidths=[doc.width*0.66, doc.width*0.34])
                    else:
                        tbl = Table([[chunk]], colWidths=[doc.width])
                    tbl.setStyle(TableStyle([
                        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor("#cfdff0")),
                        ('LEFTPADDING',(0,0),(-1,-1),8),
                        ('RIGHTPADDING',(0,0),(-1,-1),8),
                        ('TOPPADDING',(0,0),(-1,-1),6),
                        ('BOTTOMPADDING',(0,0),(-1,-1),6),
                    ]))
                    content.append(tbl)
                    content.append(Spacer(1,8))
                except Exception as e:
                    print("⚠️ table layout error for chunk, falling back to paragraphs:", e)
                    for p in chunk:
                        content.append(p)
                    content.append(Spacer(1,8))

    content.append(Paragraph("Note: Summaries auto-generated. Verify facts from original sources if needed.", styles['Meta']))
    try:
        doc.build(content)
    except Exception as e:
        print("⚠️ PDF build failed:", e)
        try:
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            c = canvas.Canvas(pdf_path, pagesize=A4)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(50, 800, "UPSC Daily Brief (Partial)")
            c.setFont("Helvetica", 10)
            c.drawString(50, 780, "Some items were skipped due to layout issues. Check action logs for details.")
            c.save()
            print("Minimal fallback PDF created.")
        except Exception as e2:
            print("Failed to create minimal PDF fallback:", e2)

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

    entries = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                entries.append({"title": e.get("title", ""), "link": e.get("link", "")})
        except Exception as ex:
            print("Feed parse error for", feed, ex)

    seen = set()
    candidates = []
    for e in entries:
        if e["link"] in seen:
            continue
        seen.add(e["link"])
        candidates.append(e)
        if len(candidates) >= MAX_CANDIDATES:
            break

    structured = []
    included = 0

    for e in candidates:
        if included >= MAX_INCLUSIONS:
            break
        title = e.get("title","")
        link = e.get("link","")
        print("Processing:", title)

        # quick UPSC keyword filter
        text_preview = ""
        try:
            r = requests.get(link, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code == 200:
                text_preview = re.sub(r'<[^>]+>', ' ', r.text)[:3000].lower()
        except Exception:
            text_preview = ""

        if not any(k in (title + " " + text_preview).lower() for k in UPSC_KEYWORDS):
            print(" -> skipped by UPSC keyword filter")
            continue

        text, img_bytes = extract_article_text_and_image(link)
        if not text:
            print(" -> no extractable text, skipping")
            continue

        parsed = groq_summarize(title, text, link)
        if parsed is None and OPENAI_API_KEY:
            print("Groq failed; trying OpenAI fallback.")
            parsed = openai_summarize(OPENAI_API_KEY, title, text, link)
        if parsed is None:
            print("Both Groq/OpenAI failed; using offline fallback.")
            parsed = offline_summary_fallback(title, text, link)
        if not parsed:
            print(" -> no parsed JSON; skipping")
            continue

        include_flag = str(parsed.get("include", "yes")).lower()
        if include_flag != "yes":
            print(" -> model marked not relevant; skipping.")
            continue

        ctx_val = (parsed.get("context") or "").strip()
        if ctx_val.upper() == "SOURCE_ONLY":
            parsed["context"] = "SOURCE_ONLY (see source)"
            parsed["key_points"] = parsed.get("key_points", []) or []
            parsed["background"] = parsed.get("background","") or ""
            parsed["impact"] = parsed.get("impact","") or ""
            parsed["upsc_relevance"] = parsed.get("upsc_relevance","")
            parsed["category"] = normalize_model_category(parsed.get("category","")) or score_category_by_keywords(title, text)[0]
        else:
            is_valid = validate_parsed(parsed)
            if not is_valid:
                print(" -> parsed JSON looks truncated; using offline fallback for safety")
                parsed = offline_summary_fallback(title, text, link)

        # choose category
        model_cat_norm = normalize_model_category(parsed.get("category","") or "")
        if model_cat_norm:
            category = model_cat_norm
            reason = "model_category"
        else:
            category, scores = score_category_by_keywords(title, text)
            reason = f"scored (scores={scores})"
        parsed["category"] = category

        # set upsc_relevance (we will show label at section header)
        parsed["upsc_relevance"] = parsed.get("upsc_relevance","") or infer_upsc_relevance_label_for_category(category)

        # attach image and normalize fields; remove source in displayed output (keep in data only)
        parsed["image_bytes"] = img_bytes
        parsed.setdefault("key_points", [])
        parsed.setdefault("background","")
        parsed.setdefault("impact","")
        parsed.setdefault("source", link)
        kp = parsed.get("key_points", [])
        if isinstance(kp, str):
            kp_list = [s.strip() for s in re.split(r'[\r\n;•\-]+', kp) if s.strip()]
            parsed["key_points"] = kp_list[:5]

        structured.append(parsed)
        included += 1
        print(f" -> included; category={category} (via {reason}); context_flag={'SOURCE_ONLY' if ctx_val.upper()=='SOURCE_ONLY' else 'OK'}")
        time.sleep(1.0)

    if not structured:
        print("No UPSC-relevant items found; building minimal fallback PDF.")
        for c in candidates[:3]:
            structured.append({
                "category":"Misc",
                "section_heading": c.get("title",""),
                "context": "Auto-added headline — no AI summary available today.",
                "background":"",
                "key_points":[c.get("title","")],
                "impact":"",
                "upsc_relevance":"",
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