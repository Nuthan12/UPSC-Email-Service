#!/usr/bin/env python3
"""
generate_and_send.py

Final UPSC Daily Brief generator (improved).
- Strong model prompt for structured Output: context, about, facts_and_policies,
  sub_sections (heading+points), impact_or_analysis, upsc_relevance.
- If model omits facts/policies, offline extractor creates them from text.
- Groups items by GS category and prints header-level UPSC label.
- Builds PDF in InsightsIAS-style layout and emails it.
"""

import os
import re
import io
import json
import time
import ssl
import smtplib
import datetime
import requests
import feedparser
from email.message import EmailMessage

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.lib import colors
from reportlab.lib.units import mm
from PIL import Image as PILImage, ImageDraw, ImageFont

# ----------------- CONFIG -----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "mixtral-8x7b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

PDF_FILENAME = f"UPSC_AI_Brief_{datetime.date.today().isoformat()}.pdf"

RSS_FEEDS = [
    "https://www.insightsonindia.com/feed",
    "https://www.drishtiias.com/feed",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://prsindia.org/theprsblog/feed",
    "https://www.thehindu.com/news/national/feeder/default.rss",
]

MAX_CANDIDATES = 40
MAX_INCLUSIONS = 12

CATEGORY_KEYWORDS = {
    "GS1": ["tribal", "caste", "society", "demography", "culture", "population", "geography", "river", "heritage"],
    "GS2": ["constitution", "cabinet", "parliament", "legislation", "supreme court", "governance", "policy", "ministry", "pib"],
    "GS3": ["economy", "gdp", "rbi", "inflation", "industry", "agriculture", "environment", "climate", "nobel", "isro", "science", "technology"],
    "GS4": ["ethics", "corruption", "integrity", "values"],
    "CME": ["study", "report", "analysis", "cme", "mains enrichment"],
    "FFP": ["facts", "prelims", "index", "statistic"],
    "Mapping": ["map", "location", "boundary", "island"],
    "Misc": []
}

CATEGORY_LABELS = {
    "GS1": "GS1 — Society / Geography",
    "GS2": "GS2 — Polity & Governance",
    "GS3": "GS3 — Economy / Science / Environment",
    "GS4": "GS4 — Ethics",
    "CME": "Content for Mains Enrichment (CME)",
    "FFP": "Facts for Prelims (FFP)",
    "Mapping": "Map-Based Learning",
    "Misc": "Miscellaneous Current Affairs"
}

# ----------------- Utilities -----------------
def safe_trim(text, max_chars=3800):
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    last_punc = max(window.rfind('.'), window.rfind('!'), window.rfind('?'))
    if last_punc > int(max_chars * 0.6):
        return window[:last_punc+1]
    last_nl = window.rfind('\n')
    if last_nl > int(max_chars * 0.5):
        return window[:last_nl].rstrip()
    return re.sub(r'\s+\S*?$', '', window)

def clean_text(raw):
    if not raw:
        return ""
    t = raw.replace("\r", "\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading"]
    for p in junk:
        t = re.sub(p, " ", t, flags=re.I)
    lines = [ln.strip() for ln in t.splitlines() if len(ln.strip())>30 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    out = "\n\n".join(lines)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()

def is_boilerplate(title, text):
    c = (title + " " + text).lower()
    patterns = ["upsc current affairs", "instalinks", "covers important current affairs of the day", "gs paper", "content for mains enrichment"]
    return sum(1 for p in patterns if p in c) >= 2

def is_india_relevant(title, text, url):
    c = (title + " " + text).lower()
    if "india" in c or "indian" in c:
        return True
    if any(d in url for d in [".gov.in", "insightsonindia", "drishtiias", "pib.gov.in", "prsindia"]):
        return True
    allow = ["nobel", "climate", "un", "summit", "report", "treaty", "agreement", "world bank", "imf"]
    return any(a in c for a in allow)

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
                except:
                    return None
    return None

# ----------------- Article extractor -----------------
def extract_article_text_and_image(url, timeout=12):
    headers = {"User-Agent":"Mozilla/5.0"}
    try:
        from newspaper import Article
        art = Article(url)
        art.download(); art.parse()
        text = clean_text(art.text or "")
        top_image = getattr(art, "top_image", None)
        img_bytes = None
        if top_image:
            try:
                r = requests.get(top_image, timeout=timeout, headers=headers)
                if r.status_code==200 and 'image' in r.headers.get('Content-Type',''):
                    img_bytes = r.content
            except:
                img_bytes = None
        if text and len(text.split())>60:
            return text, img_bytes
    except Exception:
        pass

    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        from readability import Document
        doc = Document(r.text)
        summary_html = doc.summary()
        text = clean_text(re.sub(r'<[^>]+>', ' ', summary_html))
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if text and len(text.split())>60:
            return text, img_bytes
    except Exception:
        pass

    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', ' ', r.text)
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if text and len(text.split())>60:
            return text, img_bytes
    except Exception:
        pass

    return "", None

# ----------------- Model calls -----------------
def call_api_json_groq(prompt, max_tokens=1200):
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type":"application/json"}
    payload = {"model": GROQ_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens": max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("groq error:", e)
    return None

def call_api_json_openai(prompt, max_tokens=1200):
    if not OPENAI_API_KEY:
        return None
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}
    payload = {"model": OPENAI_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens": max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("openai error:", e)
    return None

def summarize_via_model(title, url, text):
    trimmed = safe_trim(text, max_chars=3800)
    prompt = f"""
You are an expert UPSC analyst producing InsightsIAS-style current affairs notes.
Return STRICT valid JSON only in this format:

{{
  "include": "yes/no",
  "category": "GS1/GS2/GS3/GS4/CME/Mapping/FFP",
  "section_heading": "",
  "context": "",
  "about": "",
  "facts_and_policies": [],
  "sub_sections": [ {{ "heading": "", "points": [] }} ],
  "impact_or_analysis": [],
  "upsc_relevance": ""
}}

Guidelines:
- Provide concrete factual bullets (scheme names, ministry names, report titles, data, years, percentages) in facts_and_policies.
- If policy provisions / acts / rules apply, include under sub_sections (heading + points).
- Keep bullets short (6-16 words) and factual.
- If the article text is truncated, set context to "SOURCE_ONLY".
- Do not invent facts.

Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    parsed = None
    if GROQ_API_KEY:
        parsed = call_api_json_groq(prompt)
    if parsed is None and OPENAI_API_KEY:
        parsed = call_api_json_openai(prompt)
    return parsed

# ------------- Offline fact & policy extractor -------------
FACT_PATTERNS = [
    r'(\b\d{4}\b)',                                   # years
    r'(\b\d+%|\b\d+\.\d+%|\b\d+ per cent\b)',        # percentages
    r'(\b\d{1,3}(,\d{3})+\b|\b\d+\b)',                # numbers with commas or plain numbers
    r'((?:Ministry|Department|Ministries|Council|Commission)\s+of\s+[A-Z][a-zA-Z]+)', # Ministry of X
    r'([A-Z][a-z]+\s+Scheme|Scheme\s+for|Programme|Program|Act\b|Bill\b|Policy\b|Index\b|Report\b)',
    r'\b(UN|UNEP|WHO|IMF|World Bank|KAUST|ICMR|ISRO|NITI Aayog|NITI-Aayog|NITI)\b',
]

def extract_facts_from_text(text, max_bullets=6):
    bullets = []
    sentences = re.split(r'(?<=[\.\?\!])\s+', text)
    seen = set()
    # ranking sentences by matches of patterns
    scored = []
    for s in sentences:
        score = 0
        lower = s.lower()
        for pat in FACT_PATTERNS:
            if re.search(pat, s):
                score += 1
        if score>0:
            scored.append((score, s.strip()))
    scored.sort(key=lambda x: x[0], reverse=True)
    for _, s in scored:
        # shorten sentence into a bullet: if contains colon split keep second part
        bullet = s.strip()
        bullet = re.sub(r'\s+', ' ', bullet)
        if len(bullet) > 180:
            bullet = bullet[:180].rsplit(' ',1)[0] + '...'
        if bullet not in seen:
            bullets.append(bullet)
            seen.add(bullet)
        if len(bullets) >= max_bullets:
            break
    # fallback: try to pick numeric facts if none found
    if not bullets:
        nums = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_bullets]:
            if n not in seen:
                bullets.append(f"Figure: {n}")
                seen.add(n)
    return bullets

def extract_policy_mentions(text, max_items=4):
    pts = []
    sentences = re.split(r'(?<=[\.\?\!])\s+', text)
    for s in sentences:
        if re.search(r'\b(Ministry|Department|Policy|Scheme|Act|Bill|Programme|Program|NITI Aayog|PM|Prime Minister)\b', s, flags=re.I):
            p = re.sub(r'\s+', ' ', s.strip())
            if len(p) > 200:
                p = p[:200].rsplit(' ',1)[0] + '...'
            if p not in pts:
                pts.append(p)
        if len(pts) >= max_items:
            break
    return pts

# -------------- Validation & enrichment --------------
def parsed_is_good(parsed):
    if not parsed:
        return False
    inc = str(parsed.get("include","yes")).lower()
    if inc != "yes":
        return True
    ctx = (parsed.get("context","") or "").strip()
    if ctx.upper() == "SOURCE_ONLY":
        return True
    facts = parsed.get("facts_and_policies", []) or []
    # require at least one meaningful fact
    if any(len(f.strip())>8 for f in facts):
        return True
    return False

def enrich_parsed_with_offline(parsed, text):
    if parsed is None:
        parsed = {}
    facts = parsed.get("facts_and_policies", []) or []
    policies = parsed.get("sub_sections", []) or []
    if not facts or not any(len(f.strip())>8 for f in facts):
        ext_facts = extract_facts_from_text(text, max_bullets=6)
        if ext_facts:
            parsed["facts_and_policies"] = ext_facts
    # add policy mentions into sub_sections if missing
    has_policy_section = any(s.get("heading","").lower().startswith("key provision") or "policy" in s.get("heading","").lower() for s in policies)
    if not has_policy_section:
        pm = extract_policy_mentions(text, max_items=4)
        if pm:
            policies.append({"heading":"Key Provisions / Policy Mentions", "points": pm})
    parsed["sub_sections"] = policies
    return parsed

# -------------- Category scoring --------------
def score_category(title, text):
    combined = (title + " " + text).lower()
    scores = {k:0 for k in CATEGORY_KEYWORDS}
    for cat,kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            scores[cat] += combined.count(kw)
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return "Misc"
    return best[0]

# -------------- Logo generator --------------
def generate_logo_bytes(text="DailyCAThroughAI", size=(420,80), bgcolor=(31,78,121), fg=(255,255,255)):
    try:
        img = PILImage.new("RGB", size, bgcolor)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        except:
            font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0,0), text, font=font)
            w = bbox[2]-bbox[0]; h = bbox[3]-bbox[1]
        except:
            w,h = draw.textsize(text, font=font)
        x = (size[0]-w)/2; y = (size[1]-h)/2
        draw.text((x,y), text, font=font, fill=fg)
        bio = io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
        return bio.read()
    except Exception as e:
        print("logo error", e)
        return None

# -------------- PDF builder --------------
def build_pdf(structured_items, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CardTitle", fontSize=12, leading=14, spaceAfter=4))
    styles.add(ParagraphStyle(name="CardBody", fontSize=10, leading=13))
    styles.add(ParagraphStyle(name="SectionHeader", fontSize=13, leading=15, textColor=colors.HexColor("#1f4e79"), spaceBefore=8, spaceAfter=6))

    story = []
    today_str = datetime.datetime.now().strftime("%d %B %Y")
    logo_bytes = generate_logo_bytes()
    left_elem = RLImage(io.BytesIO(logo_bytes), width=110, height=28) if logo_bytes else Paragraph("DailyCAThroughAI", styles["CardBody"])
    right_elem = Paragraph(f"<b>UPSC CURRENT AFFAIRS</b><br/>{today_str}", styles["CardTitle"])
    header_table = Table([[left_elem, right_elem]], colWidths=[120, doc.width-120])
    header_table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#f4f8fb")),("LEFTPADDING",(0,0),(-1,-1),8)]))
    story.append(header_table); story.append(Spacer(1,8))

    grouped = {}
    for it in structured_items:
        cat = it.get("category","Misc")
        grouped.setdefault(cat, []).append(it)

    # order
    order = ["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    for cat in order:
        items = grouped.get(cat, [])
        if not items:
            continue
        story.append(Paragraph(CATEGORY_LABELS.get(cat, cat), styles["SectionHeader"]))
        story.append(Spacer(1,6))

        for it in items:
            parts = []
            title = it.get("section_heading","Untitled")
            parts.append(Paragraph(f"<b>{title}</b>", styles["CardTitle"]))
            ctx = it.get("context","")
            if ctx:
                parts.append(Paragraph(f"<b>Context:</b> {ctx}", styles["CardBody"]))
            about = it.get("about","")
            if about:
                parts.append(Paragraph(f"<b>About:</b> {about}", styles["CardBody"]))

            facts = it.get("facts_and_policies", []) or []
            if facts:
                parts.append(Paragraph("<b>Facts & Data:</b>", styles["CardBody"]))
                for f in facts:
                    parts.append(Paragraph(f"• {f}", styles["CardBody"]))

            subs = it.get("sub_sections", []) or []
            for s in subs:
                head = s.get("heading","")
                pts = s.get("points",[]) or []
                if head:
                    parts.append(Spacer(1,4))
                    parts.append(Paragraph(f"<b>{head}:</b>", styles["CardBody"]))
                for p in pts:
                    parts.append(Paragraph(f"• {p}", styles["CardBody"]))

            impact = it.get("impact_or_analysis",[]) or []
            if impact:
                parts.append(Paragraph("<b>Impact / Analysis:</b>", styles["CardBody"]))
                for im in impact:
                    parts.append(Paragraph(f"• {im}", styles["CardBody"]))

            # image on right if available
            img_elem = None
            im_bytes = it.get("image_bytes")
            if im_bytes:
                try:
                    pil = PILImage.open(io.BytesIO(im_bytes))
                    pil.thumbnail((150,100))
                    bb = io.BytesIO(); pil.save(bb, format="PNG"); bb.seek(0)
                    img_elem = RLImage(bb, width=min(140,pil.width), height=min(100,pil.height))
                except:
                    img_elem = None
            try:
                if img_elem:
                    tbl = Table([[parts, img_elem]], colWidths=[doc.width*0.66, doc.width*0.34])
                else:
                    tbl = Table([[parts]], colWidths=[doc.width])
                tbl.setStyle(TableStyle([
                    ("BOX",(0,0),(-1,-1),0.5, colors.HexColor("#cfdff0")),
                    ("LEFTPADDING",(0,0),(-1,-1),8),
                    ("RIGHTPADDING",(0,0),(-1,-1),8),
                    ("TOPPADDING",(0,0),(-1,-1),6),
                    ("BOTTOMPADDING",(0,0),(-1,-1),6),
                ]))
                story.append(tbl)
            except Exception as e:
                for p in parts:
                    story.append(p)
            story.append(Spacer(1,10))

    story.append(Paragraph("Note: Summaries auto-generated; verify facts from original sources if needed.", ParagraphStyle(name="note", fontSize=8, textColor=colors.grey)))
    try:
        doc.build(story)
        return out_path
    except Exception as e:
        print("PDF build failed:", e)
        try:
            from reportlab.pdfgen import canvas
            c = canvas.Canvas(out_path, pagesize=A4)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(50,800,"UPSC Daily Brief (Partial)")
            c.setFont("Helvetica", 10)
            c.drawString(50,780,"Some items were skipped due to layout issues. Check logs.")
            c.save()
            return out_path
        except Exception as e2:
            print("PDF fallback failed", e2)
            return None

# ----------------- Email -----------------
def email_pdf_file(path):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("SMTP_USER / SMTP_PASSWORD / EMAIL_TO must be set as env vars")
    msg = EmailMessage()
    msg["Subject"] = f"UPSC AI Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content("Attached: UPSC AI Current Affairs Brief (auto-generated).")

    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(path))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# --------------- Main pipeline -----------------
def main():
    print("Start generating:", datetime.datetime.utcnow().isoformat())
    candidates = []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for entry in parsed.entries[:15]:
                title = entry.get("title",""); link = entry.get("link","")
                if title and link:
                    candidates.append({"title":title, "link":link})
                if len(candidates) >= MAX_CANDIDATES:
                    break
        except Exception as e:
            print("feed error", feed, e)
    print("Collected candidates:", len(candidates))

    structured = []
    seen = set()
    included = 0
    for c in candidates:
        if included >= MAX_INCLUSIONS:
            break
        title = c["title"].strip(); link = c["link"]
        if link in seen:
            continue
        seen.add(link)
        print("Processing:", title)
        text, img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text")
            continue
        if is_boilerplate(title, text):
            print(" -> boilerplate skipped")
            continue
        if not is_india_relevant(title, text, link):
            print(" -> skipped not India-relevant")
            continue

        parsed = summarize_via_model(title, link, text)
        if parsed is None:
            # basic fallback structure
            sents = [s.strip() for s in re.split(r'\.|\n', text) if len(s.strip())>40]
            parsed = {
                "include":"yes",
                "category":"",
                "section_heading": title,
                "context": sents[0] if sents else title,
                "about": " ".join(sents[1:3]) if len(sents)>1 else "",
                "facts_and_policies": [],
                "sub_sections": [],
                "impact_or_analysis": [],
                "upsc_relevance":""
            }

        # If parsed lacks facts or looks truncated, enrich offline
        if not parsed_is_good(parsed):
            print(" -> parsed lacks facts; enriching offline")
            parsed = enrich_parsed_with_offline(parsed, text)

        # final safety: ensure some facts exist
        if not parsed.get("facts_and_policies"):
            parsed["facts_and_policies"] = extract_facts_from_text(text, max_bullets=5)

        # category
        cat = parsed.get("category","") or ""
        if cat:
            m = re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
            if m:
                category = f"GS{m.group(1)}"
            else:
                category = cat
        else:
            category = score_category(title, text)
        if category not in CATEGORY_LABELS:
            category = "Misc"
        parsed["category"] = category

        # attach image
        parsed["image_bytes"] = img
        parsed.setdefault("sub_sections", [])
        parsed.setdefault("impact_or_analysis", [])
        structured.append(parsed)
        included += 1
        print(f" -> included as {category}; total {included}")
        time.sleep(0.7)

    if not structured:
        print("No relevant items found; creating minimal placeholder and aborting.")
        return

    out_path = PDF_FILENAME
    pdf_created = build_pdf(structured, out_path)
    if not pdf_created:
        print("PDF creation failed")
        return
    print("PDF created:", out_path)

    try:
        email_pdf_file(out_path)
    except Exception as e:
        print("Email failed:", e)
        return
    print("Done.")

if __name__ == "__main__":
    main()