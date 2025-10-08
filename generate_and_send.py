#!/usr/bin/env python3
"""
generate_and_send.py

UPSC Daily Brief generator — safe_trim + post-parse validation + improved classification.

Features:
- Groq primary summarization, OpenAI fallback, offline fallback
- safe_trim to avoid truncating sentences mid-word
- scoring-based classifier + extended keyword lists (better GS1/GS3 detection)
- post-parse validation (detect truncated outputs and fallback)
- robust PDF layout, logo, safe image handling, chunking, email
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

# Category keywords for scoring classifier (expanded to detect geography/science)
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
    junk_patterns = [
        r"SEE ALL NEWSLETTERS", r"e-?Paper", r"ADVERTISEMENT", r"LOGIN", r"Subscribe",
        r"Related Stories", r"Continue reading", r"Read more", r"Click here"
    ]
    for p in junk_patterns:
        text = re.sub(p, " ", text, flags=re.I)
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    cleaned = "\n\n".join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def safe_trim(text, max_chars=3500):
    """
    Trim to max_chars but avoid cutting mid-sentence.
    Return up to the last period within the window (if reasonably far in),
    else return the window itself.
    """
    if not text or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    # find last sentence end
    last_period = max(window.rfind('.'), window.rfind('!'), window.rfind('?'))
    if last_period and last_period > int(max_chars * 0.6):
        return window[:last_period+1]
    # fallback: try to break at last newline
    last_nl = window.rfind('\n')
    if last_nl and last_nl > int(max_chars * 0.5):
        return window[:last_nl].rstrip()
    # final fallback: return window but strip trailing partial word
    if window and window[-1].isalnum():
        # drop trailing partial token
        return re.sub(r'\s+\S*?$', '', window)
    return window

def extract_article_text_and_image(url, timeout=12):
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

# ---------------- Classification & relevance ----------------
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
            # simple occurrence count with small weight for longer keywords
            cnt = combined.count(kw)
            if cnt:
                scores[cat] += cnt * (1.0 + min(len(kw) / 12.0, 1.5))
    sorted_scores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = sorted_scores[0]
    if best_score <= 0:
        return "Misc", scores
    # check closeness to second best
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    if second_score >= 0.7 * best_score:
        priority = ["GS2", "GS3", "GS1", "GS4", "CME", "FFP", "Mapping", "Misc"]
        for p in priority:
            if abs(scores.get(p, 0) - best_score) < 1e-6 or scores.get(p, 0) == best_score:
                return p, scores
        return best_cat, scores
    return best_cat, scores

def infer_upsc_relevance(parsed, title, text):
    upsc_rel = (parsed.get("upsc_relevance") or "").strip()
    if upsc_rel and len(upsc_rel) > 6 and upsc_rel.lower() not in ["general current affairs", "general", "n/a", ""]:
        return upsc_rel
    model_cat_norm = normalize_model_category(parsed.get("category", "") or "")
    if model_cat_norm:
        if model_cat_norm == "GS1":
            return "GS1 — Society/Geography"
        if model_cat_norm == "GS2":
            return "GS2 — Polity & Governance"
        if model_cat_norm == "GS3":
            return "GS3 — Economy/Science/Environment"
        if model_cat_norm == "GS4":
            return "GS4 — Ethics"
        if model_cat_norm == "CME":
            return "CME — Studies & Reports"
        if model_cat_norm == "Mapping":
            return "Mapping — Geography/Location"
        if model_cat_norm == "FFP":
            return "FFP — Public Finance"
    combined = (title + " " + text).lower()
    for key, (label, kws) in RELEVANCE_KEYWORDS.items():
        for kw in kws:
            if kw in combined:
                return label
    cat, scores = score_category_by_keywords(title, text)
    if cat == "GS1":
        return "GS1 — Society/Geography"
    if cat == "GS2":
        return "GS2 — Polity & Governance"
    if cat == "GS3":
        return "GS3 — Economy/Science/Environment"
    if cat == "GS4":
        return "GS4 — Ethics"
    if cat == "CME":
        return "CME — Studies & Reports"
    if cat == "Mapping":
        return "Mapping — Geography/Location"
    if cat == "FFP":
        return "FFP — Public Finance"
    return "General Current Affairs"

# ---------------- GROQ summarization ----------------
def call_groq_with_retries(url, headers, payload, attempts=3, backoff=3, timeout=90):
    for i in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            print(f"[groq] attempt {i+1} status {r.status_code}")
            body_snippet = r.text[:1000].replace("\n", " ")
            if len(body_snippet) > 0:
                print("[groq] body_snippet:", body_snippet[:800])
            return r
        except Exception as ex:
            print(f"[groq] exception attempt {i+1}:", ex)
            time.sleep(backoff * (i + 1))
    return None

def groq_summarize(title, text, url, timeout=90):
    if not GROQ_API_KEY:
        print("No GROQ_API_KEY found.")
        return None
    # use safe_trim to avoid sending mid-sentence text to the model
    trimmed = safe_trim(text, max_chars=3500)
    prompt = f"""
You are a UPSC current-affairs analyst producing InsightsIAS-style notes.
Respond STRICTLY with valid JSON only and nothing else. Keys required:
include, category, section_heading, context, background, key_points (list), impact, upsc_relevance, source

Article title: {title}
Article URL: {url}
Article text (trimmed, keep sentences intact): {trimmed}

Decide include: "yes" if relevant for UPSC (GS papers or FFP/CME/Mapping), otherwise "no".
Important: do not invent facts. If the article text is incomplete/truncated, state "SOURCE_ONLY" in 'context' and keep other fields empty or minimal, and set include to "yes" if source itself is relevant.
"""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 900}
    r = call_groq_with_retries(GROQ_ENDPOINT, headers, payload, attempts=3, backoff=3, timeout=timeout)
    if not r:
        print("Groq: no response after retries.")
        return None
    if r.status_code != 200:
        print("Groq returned non-200:", r.status_code, getattr(r, "text", "")[:1000])
        return None
    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        parsed = extract_json_substring(content)
        return parsed
    except Exception as ex:
        print("Groq parse error:", ex, getattr(r, "text", "")[:800])
        return None

# ---------------- OpenAI fallback ----------------
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

If text seems truncated, set context to "SOURCE_ONLY" and include minimal fields.
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

# ---------------- Offline fallback ----------------
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

# ---------------- Post-parse validation ----------------
def looks_truncated(text):
    """Return True if text appears truncated: ends with hyphen or last token is partial/digit or ends mid-word."""
    if not text:
        return False
    t = text.strip()
    # ends with a hyphen meaning likely cut
    if t.endswith('-') or t.endswith('—'):
        return True
    # ends with a word fragment (last char is alphanumeric but previous char is not space) - crude
    if re.search(r'\w{0,3}$', t) and (len(t) > 0 and t[-1].isalnum() and not t.endswith('.')):
        # check if ends with a number likely truncated e.g. "6"
        if re.search(r'\d+$', t):
            return True
    # ends with incomplete clause (no terminal punctuation and length is long)
    if len(t) > 120 and not re.search(r'[\.!?]$', t):
        return True
    return False

def validate_parsed(parsed):
    """
    Ensure parsed JSON has full sentences in critical fields.
    If any core field looks truncated, return False.
    """
    if not parsed:
        return False
    # context and background and key_points should be present if include=yes
    include = str(parsed.get("include", "yes")).lower()
    if include != "yes":
        return True  # it's intentionally excluded
    # if context is SOURCE_ONLY, allow but mark as valid (we'll handle specially)
    context = parsed.get("context", "") or ""
    if context.strip().upper() == "SOURCE_ONLY":
        return True
    background = parsed.get("background", "") or ""
    # check for truncation
    if looks_truncated(context) or looks_truncated(background):
        return False
    # key_points: ensure each bullet not truncated
    kps = parsed.get("key_points", []) or []
    for kp in kps:
        if looks_truncated(kp):
            return False
    return True

# ---------------- Logo generator & PDF builder & email (as before, robust) ----------------
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

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleLarge", fontSize=16, alignment=1, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name="Section", fontSize=12, leading=14, spaceAfter=4, spaceBefore=8, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name="Body", fontSize=10, leading=13))
    styles.add(ParagraphStyle(name="Meta", fontSize=8, leading=10, textColor=colors.grey))

    content = []
    today_str = datetime.datetime.now().strftime("%d %B %Y")

    logo_bytes = generate_logo_bytes()
    left_elem = None
    if logo_bytes:
        try:
            left_elem = RLImage(io.BytesIO(logo_bytes), width=110, height=28)
        except Exception:
            left_elem = Paragraph("DailyCAThroughAI", styles["Body"])
    else:
        left_elem = Paragraph("DailyCAThroughAI", styles["Body"])
    right = Paragraph(f"<b>UPSC CURRENT AFFAIRS</b><br/>{today_str}", styles["TitleLarge"])
    header_table = Table([[left_elem, right]], colWidths=[120, doc.width - 120])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (1, 0), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f8fb")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    content.append(header_table)
    content.append(Spacer(1, 8))

    order = ["GS2", "GS3", "GS1", "GS4", "CME", "FFP", "Mapping", "Misc"]
    for cat in order:
        items = [it for it in structured_items if it.get("category") == cat]
        if not items:
            continue
        content.append(Paragraph(cat, styles["Section"]))
        for it in items:
            paras = []
            paras.append(Paragraph(f"<b>{it.get('section_heading','')}</b>", styles["Body"]))
            if it.get("context"):
                paras.append(Paragraph(f"<i>Context:</i> {it.get('context')}", styles["Body"]))
            if it.get("background"):
                paras.append(Paragraph(f"<b>Background:</b> {it.get('background')}", styles["Body"]))
            for kp in it.get("key_points", []):
                paras.append(Paragraph(f"• {kp}", styles["Body"]))
            if it.get("impact"):
                paras.append(Paragraph(f"<b>Impact/Significance:</b> {it.get('impact')}", styles["Body"]))
            upsc_rel = it.get("upsc_relevance", "")
            if upsc_rel:
                paras.append(Paragraph(f"<b>UPSC Relevance:</b> {upsc_rel}", styles["Body"]))
            if it.get("source"):
                paras.append(Paragraph(f"Source: {it.get('source')}", styles["Meta"]))

            chunk_size = 6
            chunks = [paras[i:i + chunk_size] for i in range(0, len(paras), chunk_size)]

            imgb = it.get("image_bytes")
            right_col_first = None
            if imgb:
                try:
                    img = PILImage.open(io.BytesIO(imgb))
                    img.load()
                    w, h = img.size
                    if w <= 0 or h <= 0 or w > 20000 or h > 20000:
                        raise ValueError("image dimensions suspicious")
                    max_w, max_h = 180, 120
                    if w > max_w or h > max_h:
                        img.thumbnail((max_w, max_h))
                    aspect = (img.height / img.width) if img.width else 1
                    if aspect > 6 or aspect < 0.05:
                        raise ValueError("unusual aspect ratio")
                    bb = io.BytesIO()
                    img.save(bb, format="PNG")
                    bb.seek(0)
                    img_w, img_h = img.size
                    right_col_first = RLImage(bb, width=min(img_w, 150), height=min(img_h, 100))
                except Exception as e:
                    print("⚠️ image skipped for article:", e)
                    right_col_first = None

            for idx, chunk in enumerate(chunks):
                try:
                    if idx == 0 and right_col_first:
                        tbl = Table([[chunk, right_col_first]], colWidths=[doc.width * 0.66, doc.width * 0.34])
                    else:
                        tbl = Table([[chunk]], colWidths=[doc.width])
                    tbl.setStyle(
                        TableStyle(
                            [
                                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cfdff0")),
                                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                ("TOPPADDING", (0, 0), (-1, -1), 6),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                            ]
                        )
                    )
                    content.append(tbl)
                    content.append(Spacer(1, 8))
                except Exception as e:
                    print("⚠️ table layout error for chunk, falling back to paragraphs:", e)
                    for p in chunk:
                        content.append(p)
                    content.append(Spacer(1, 8))

    content.append(Paragraph("Note: Summaries auto-generated. Verify facts from original sources (PIB/The Hindu).", styles["Meta"]))

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
        title = e.get("title", "")
        link = e.get("link", "")
        print("Processing:", title)

        # quick UPSC keyword filter (lightweight quick-check)
        text_preview = ""
        try:
            r = requests.get(link, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
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

        # 1) Try Groq
        parsed = groq_summarize(title, text, link)
        # 2) OpenAI fallback
        if parsed is None and OPENAI_API_KEY:
            print("Groq failed; trying OpenAI fallback.")
            parsed = openai_summarize(OPENAI_API_KEY, title, text, link)
        # 3) offline fallback
        if parsed is None:
            print("Both Groq/OpenAI failed; using offline fallback.")
            parsed = offline_summary_fallback(title, text, link)

        if not parsed:
            print(" -> no parsed JSON; skipping")
            continue

        # ensure include decision
        include_flag = str(parsed.get("include", "yes")).lower()
        if include_flag != "yes":
            print(" -> model marked not relevant; skipping.")
            continue

        # if parsed indicates SOURCE_ONLY in context, we keep but mark
        ctx_val = (parsed.get("context") or "").strip()
        if ctx_val.upper() == "SOURCE_ONLY":
            # attach source prominently and proceed (this indicates text was incomplete)
            parsed["context"] = "SOURCE_ONLY (see source link)"
            parsed["key_points"] = parsed.get("key_points", []) or []
            parsed["background"] = parsed.get("background", "") or ""
            parsed["impact"] = parsed.get("impact", "") or ""
            parsed["upsc_relevance"] = infer_upsc_relevance(parsed, title, text)
            parsed["category"] = normalize_model_category(parsed.get("category","")) or score_category_by_keywords(title, text)[0]
        else:
            # validate parsed; if looks truncated, fallback to offline summary or mark SOURCE_ONLY
            is_valid = validate_parsed(parsed)
            if not is_valid:
                print(" -> parsed JSON looks truncated; using offline fallback for safety")
                parsed = offline_summary_fallback(title, text, link)

        # determine category: prefer normalized model category if mappable, else score
        model_cat_norm = normalize_model_category(parsed.get("category", "") or "")
        if model_cat_norm:
            category = model_cat_norm
            reason = "model_category"
        else:
            category, scores = score_category_by_keywords(title, text)
            reason = f"scored (scores={scores})"
        parsed["category"] = category

        # final upsc relevance inference
        parsed["upsc_relevance"] = infer_upsc_relevance(parsed, title, text)

        # attach image and normalize fields
        parsed["image_bytes"] = img_bytes
        parsed.setdefault("key_points", [])
        parsed.setdefault("background", "")
        parsed.setdefault("impact", "")
        parsed.setdefault("source", link)
        kp = parsed.get("key_points", [])
        if isinstance(kp, str):
            kp_list = [s.strip() for s in re.split(r'[\r\n;•\-]+', kp) if s.strip()]
            parsed["key_points"] = kp_list[:5]

        structured.append(parsed)
        included += 1
        print(f" -> included; category={category} (via {reason}); relevance={parsed.get('upsc_relevance')}")
        time.sleep(1.0)

    if not structured:
        print("No UPSC-relevant items found; building minimal fallback PDF.")
        for c in candidates[:3]:
            structured.append({
                "category": "Misc",
                "section_heading": c.get("title", ""),
                "context": "Auto-added headline — no AI summary available today.",
                "background": "",
                "key_points": [c.get("title", "")],
                "impact": "",
                "upsc_relevance": "",
                "source": c.get("link", ""),
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