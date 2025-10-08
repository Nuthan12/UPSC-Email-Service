#!/usr/bin/env python3
"""
generate_and_send.py — improved PDF layout + stronger content enforcement

Revisions:
- PDF layout: uses KeepTogether and ListFlowable/ListItem for consistent card layout,
  two-column (text left, image right) without overflowing table cells.
- Content: model prompt now requests 'detailed_brief' and 'policy_points'; offline
  enrichment ensures minimum lengths for 'about' and 'detailed_brief'.
- Other features: boilerplate filter, India relevance, Groq/OpenAI fallback, email.
"""

import os, re, io, json, time, ssl, smtplib, datetime, requests, feedparser
from email.message import EmailMessage

# PDF / imaging
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage,
    KeepTogether, ListFlowable, ListItem, PageBreak
)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from PIL import Image as PILImage, ImageDraw, ImageFont

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "mixtral-8x7b")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

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

# ---------------- Helpers ----------------
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
    patterns = ["upsc current affairs", "instalinks", "think beyond the current affairs", "covers important current affairs", "gs paper"]
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

# --------------- extraction ----------------
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
    # readability fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        from readability import Document
        doc = Document(r.text)
        summary_html = doc.summary()
        text = clean_text(re.sub(r'<[^>]+>', ' ', summary_html))
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        img_bytes = None
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
    # simple fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', ' ', r.text)
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        img_bytes = None
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if text and len(text.split())>60:
            return text, img_bytes
    except:
        pass
    return "", None

# --------------- Model calls ----------------
def call_model(prompt, use_groq=True, max_tokens=1200):
    # try groq
    if use_groq and GROQ_API_KEY:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type":"application/json"}
            payload = {"model":GROQ_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
            r = requests.post(url, json=payload, headers=headers, timeout=60)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                parsed = extract_json_substring(content)
                if parsed: return parsed
        except Exception as e:
            print("groq error", e)
    # fallback openai
    if OPENAI_API_KEY:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}
            payload = {"model":OPENAI_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
            r = requests.post(url, json=payload, headers=headers, timeout=60)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                parsed = extract_json_substring(content)
                if parsed: return parsed
        except Exception as e:
            print("openai error", e)
    return None

def summarize_via_model(title, url, text):
    trimmed = safe_trim(text, max_chars=3800)
    prompt = f"""
You are an expert UPSC analyst writing InsightsIAS-style notes.
Return STRICT valid JSON only in this format:

{{
  "include":"yes/no",
  "category":"GS1/GS2/GS3/GS4/CME/Mapping/FFP",
  "section_heading":"",
  "context":"",
  "about":"",
  "facts_and_policies": [],
  "policy_points": [],          # concise policy/scheme/act bullets
  "detailed_brief": "",         # 120-220 words paragraph
  "sub_sections": [ {{ "heading":"", "points": [] }} ],
  "impact_or_analysis": [],
  "upsc_relevance":""
}}

Guidelines:
- Put 3-6 factual bullets (schemes, ministries, reports, years, numbers) in facts_and_policies.
- If policy or legal provisions exist, list them in policy_points and/or sub_sections.
- Provide a 'detailed_brief' paragraph (120-220 words) synthesizing causes, facts and implications for UPSC.
- If article is truncated, set context to "SOURCE_ONLY".
- Do not invent facts.

Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    return call_model(prompt)

# --------------- offline extraction helpers ---------------
FACT_PATTERNS = [
    r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b\d{1,3}(?:,\d{3})+\b', r'\b(ICMR|ISRO|NITI Aayog|WHO|World Bank|UN|KAUST|IMF)\b',
    r'\b(Ministry of|Department of|Scheme|Policy|Act|Bill|programme|program)\b', r'\b(report|index|survey)\b'
]

def extract_facts_from_text(text, max_bullets=6):
    bullets=[]
    sentences = re.split(r'(?<=[\.\?\!])\s+', text)
    scored=[]
    for s in sentences:
        score=0
        for pat in FACT_PATTERNS:
            if re.search(pat, s, flags=re.I):
                score+=1
        if score>0:
            scored.append((score, s.strip()))
    scored.sort(key=lambda x: x[0], reverse=True)
    seen=set()
    for _, s in scored:
        b = re.sub(r'\s+', ' ', s)
        if len(b)>200: b = b[:200].rsplit(' ',1)[0]+'...'
        if b not in seen:
            bullets.append(b)
            seen.add(b)
        if len(bullets)>=max_bullets: break
    if not bullets:
        nums = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_bullets]:
            bullets.append(f"Figure: {n}")
    return bullets

def extract_policy_points(text, max_items=4):
    pts=[]
    sentences = re.split(r'(?<=[\.\?\!])\s+', text)
    for s in sentences:
        if re.search(r'\b(Ministry|Department|Policy|Scheme|Act|Bill|NITI Aayog|PM|Prime Minister)\b', s, flags=re.I):
            p = re.sub(r'\s+', ' ', s.strip())
            if len(p)>200: p = p[:200].rsplit(' ',1)[0]+'...'
            if p not in pts:
                pts.append(p)
        if len(pts)>=max_items: break
    return pts

# --------------- validation & enrichment ---------------
def parsed_needs_enrichment(parsed):
    if not parsed:
        return True
    facts = parsed.get("facts_and_policies") or []
    detailed = (parsed.get("detailed_brief") or "").strip()
    about = (parsed.get("about") or "").strip()
    # require at least 2 facts and a detailed_brief > 80 chars
    if len([f for f in facts if len(f.strip())>8]) < 2 or len(detailed) < 80 or len(about) < 40:
        return True
    return False

def enrich_parsed(parsed, text, title):
    if parsed is None:
        parsed = {"section_heading": title, "facts_and_policies": [], "policy_points": [], "sub_sections": [], "impact_or_analysis": [], "about": "", "detailed_brief": ""}
    # fill facts if missing
    facts = parsed.get("facts_and_policies", []) or []
    if len([f for f in facts if len(f.strip())>8]) < 2:
        ext = extract_facts_from_text(text, max_bullets=6)
        parsed["facts_and_policies"] = (facts + ext)[:6]
    # fill policy_points
    ppts = parsed.get("policy_points", []) or []
    if not ppts:
        ppts = extract_policy_points(text, max_items=4)
        parsed["policy_points"] = ppts
        if ppts:
            subs = parsed.get("sub_sections", []) or []
            subs.append({"heading":"Key Provisions / Policy Mentions","points": ppts})
            parsed["sub_sections"] = subs
    # generate a short detailed_brief if still missing using a lightweight template
    if not parsed.get("detailed_brief") or len(parsed.get("detailed_brief","").strip()) < 80:
        sb = []
        if parsed.get("about"):
            sb.append(parsed["about"])
        sb.extend(parsed.get("facts_and_policies",[])[:3])
        brief = " ".join(sb)
        # keep brief concise if long
        if len(brief) < 120:
            # expand by joining a couple more facts
            brief = brief + " " + " ".join(parsed.get("policy_points",[])[:2])
        if len(brief) < 80:
            brief = (parsed.get("section_heading","") + ". ") + brief
        parsed["detailed_brief"] = brief[:800]
    return parsed

# --------------- category scoring ---------------
def score_category(title, text):
    combined = (title + " " + text).lower()
    scores = {k:0 for k in CATEGORY_KEYWORDS}
    for cat,kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            scores[cat] += combined.count(kw)
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1]==0:
        return "Misc"
    return best[0]

# --------------- logo ---------------
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
        print("logo error", e); return None

# --------------- PDF builder (improved) ---------------
def build_pdf(structured_items, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CardTitle", fontSize=12, leading=14, spaceAfter=4))
    styles.add(ParagraphStyle(name="CardBody", fontSize=10, leading=13))
    styles.add(ParagraphStyle(name="SectionHeader", fontSize=13, leading=15, textColor=colors.HexColor("#1f4e79"), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="CenteredSmall", fontSize=9, leading=11, alignment=TA_CENTER, textColor=colors.grey))

    story = []
    today_str = datetime.datetime.now().strftime("%d %B %Y")
    logo_bytes = generate_logo_bytes()
    left_elem = RLImage(io.BytesIO(logo_bytes), width=110, height=28) if logo_bytes else Paragraph("DailyCAThroughAI", styles["CardBody"])
    right_elem = Paragraph(f"<b>UPSC CURRENT AFFAIRS</b><br/>{today_str}", styles["CardTitle"])
    header_table = Table([[left_elem, right_elem]], colWidths=[120, doc.width-120])
    header_table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#f4f8fb")),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8)]))
    story.append(header_table); story.append(Spacer(1,8))

    # group by category and keep order
    order = ["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    grouped = {c:[] for c in order}
    for it in structured_items:
        cat = it.get("category","Misc")
        if cat not in grouped: grouped["Misc"].append(it)
        else: grouped[cat].append(it)

    for cat in order:
        items = grouped.get(cat, [])
        if not items:
            continue
        # section header with friendly label
        label = CATEGORY_LABELS.get(cat, cat)
        story.append(Paragraph(label, styles["SectionHeader"]))
        story.append(Spacer(1,6))

        for it in items:
            # Build left column flowables
            left_flow = []
            title = it.get("section_heading","Untitled")
            left_flow.append(Paragraph(f"<b>{title}</b>", styles["CardTitle"]))
            if it.get("context"):
                left_flow.append(Paragraph(f"<b>Context:</b> {it['context']}", styles["CardBody"]))
            if it.get("about"):
                left_flow.append(Paragraph(f"<b>About:</b> {it['about']}", styles["CardBody"]))

            facts = it.get("facts_and_policies",[]) or []
            if facts:
                left_flow.append(Paragraph("<b>Facts & Data:</b>", styles["CardBody"]))
                # use ListFlowable for bullets to ensure proper indentation & wrapping
                bullets = [ListItem(Paragraph(f, styles["CardBody"]), leftIndent=6) for f in facts]
                left_flow.append(ListFlowable(bullets, bulletType='bullet', start='•', leftIndent=8))

            policy_pts = it.get("policy_points", []) or []
            subs = it.get("sub_sections",[]) or []
            # include sub_sections too
            for s in subs:
                head = s.get("heading","")
                pts = s.get("points",[]) or []
                if head:
                    left_flow.append(Spacer(1,4))
                    left_flow.append(Paragraph(f"<b>{head}:</b>", styles["CardBody"]))
                    bullets = [ListItem(Paragraph(p, styles["CardBody"]), leftIndent=6) for p in pts]
                    left_flow.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))

            if policy_pts and not subs:
                left_flow.append(Paragraph("<b>Policy / Provisions:</b>", styles["CardBody"]))
                bullets = [ListItem(Paragraph(p, styles["CardBody"]), leftIndent=6) for p in policy_pts]
                left_flow.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))

            detailed = it.get("detailed_brief","")
            if detailed:
                left_flow.append(Spacer(1,4))
                left_flow.append(Paragraph(f"<b>Detailed Brief:</b> {detailed}", styles["CardBody"]))

            impact = it.get("impact_or_analysis",[]) or []
            if impact:
                left_flow.append(Spacer(1,4))
                left_flow.append(Paragraph("<b>Impact / Analysis:</b>", styles["CardBody"]))
                bullets = [ListItem(Paragraph(p, styles["CardBody"]), leftIndent=6) for p in impact]
                left_flow.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))

            # KeepTogether ensures the left flow stays intact in page breaks where feasible
            left_block = KeepTogether(left_flow)

            # prepare right image if any
            img_elem = None
            im_bytes = it.get("image_bytes")
            if im_bytes:
                try:
                    pil = PILImage.open(io.BytesIO(im_bytes))
                    pil.thumbnail((150,100))
                    bb = io.BytesIO(); pil.save(bb, format='PNG'); bb.seek(0)
                    img_elem = RLImage(bb, width=min(140,pil.width), height=min(100,pil.height))
                except Exception:
                    img_elem = None

            # Build a 2-col table: left_block and optional image. If no image, single column
            try:
                if img_elem:
                    tbl = Table([[left_block, img_elem]], colWidths=[doc.width*0.68, doc.width*0.32])
                else:
                    tbl = Table([[left_block]], colWidths=[doc.width])
                tbl.setStyle(TableStyle([
                    ("BOX",(0,0),(-1,-1),0.5, colors.HexColor("#cfdff0")),
                    ("LEFTPADDING",(0,0),(-1,-1),8),
                    ("RIGHTPADDING",(0,0),(-1,-1),8),
                    ("TOPPADDING",(0,0),(-1,-1),6),
                    ("BOTTOMPADDING",(0,0),(-1,-1),6),
                ]))
                story.append(tbl)
            except Exception as e:
                # fallback: append left_block alone
                story.append(left_block)
                story.append(Spacer(1,4))
            story.append(Spacer(1,10))

    # footer
    note_style = ParagraphStyle("note", fontSize=8, textColor=colors.grey, alignment=TA_LEFT)
    story.append(Paragraph("Note: Summaries auto-generated. Verify facts from original sources if needed.", note_style))
    # build
    try:
        doc.build(story)
        return out_path if (out_path:=out_path) else out_path
    except Exception as e:
        print("PDF build error:", e)
        # minimal fallback
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
            print("Minimal fallback failed:", e2)
            return None

# ---------------- email ----------------
def email_pdf_file(path):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("Set SMTP_USER / SMTP_PASSWORD / EMAIL_TO")
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

# -------------- main pipeline ----------------
def main():
    print("Start:", datetime.datetime.utcnow().isoformat())
    # collect RSS
    candidates=[]
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for entry in parsed.entries[:12]:
                title = entry.get("title",""); link = entry.get("link","")
                if title and link:
                    candidates.append({"title":title, "link":link})
                if len(candidates) >= MAX_CANDIDATES:
                    break
        except Exception as e:
            print("feed error", feed, e)
    print("Candidates:", len(candidates))

    structured=[]
    seen=set(); included=0
    for c in candidates:
        if included >= MAX_INCLUSIONS: break
        title = c["title"].strip(); link = c["link"]
        if link in seen: continue
        seen.add(link)
        print("Processing:", title)
        text,img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text, skip"); continue
        if is_boilerplate(title, text):
            print(" -> boilerplate skip"); continue
        if not is_india_relevant(title, text, link):
            print(" -> not India-relevant skip"); continue

        parsed = summarize_via_model(title, link, text)
        if parsed is None:
            # fallback structure
            sents = [s.strip() for s in re.split(r'\.|\n', text) if len(s.strip())>40]
            parsed = {
                "include":"yes","category":"","section_heading":title,
                "context": sents[0] if sents else title,
                "about": " ".join(sents[1:3]) if len(sents)>1 else "",
                "facts_and_policies": [], "policy_points": [], "detailed_brief":"",
                "sub_sections": [], "impact_or_analysis": [], "upsc_relevance":""
            }

        # enrichment if needed
        if parsed_needs_enrichment(parsed):
            print(" -> enriching parsed content offline")
            parsed = enrich_parsed(parsed, text, title)

        # ensure minimal facts
        if not parsed.get("facts_and_policies"):
            parsed["facts_and_policies"] = extract_facts_from_text(text, max_bullets=5)

        # category mapping
        cat = parsed.get("category","") or ""
        if cat:
            m = re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
            if m: category = f"GS{m.group(1)}"
            else: category = cat
        else:
            category = score_category(title, text)
        if category not in CATEGORY_LABELS:
            category = "Misc"
        parsed["category"] = category

        parsed["image_bytes"] = img
        parsed.setdefault("sub_sections", [])
        parsed.setdefault("impact_or_analysis", [])
        structured.append(parsed)
        included += 1
        print(f" -> included as {category} ({included})")
        time.sleep(0.6)

    if not structured:
        print("No items; abort.")
        return

    out_path = PDF_FILENAME
    pdf_created = build_pdf(structured, out_path)
    if not pdf_created:
        print("PDF failed")
        return
    print("PDF:", out_path)

    try:
        email_pdf_file(out_path)
    except Exception as e:
        print("Email failed:", e)
        return
    print("Done.")

if __name__ == "__main__":
    main()