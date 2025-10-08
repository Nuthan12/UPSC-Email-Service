#!/usr/bin/env python3
"""
generate_and_send.py - Guaranteed-content UPSC Brief generator

Behavior:
- Scrapes candidate articles from RSS feeds.
- Tries Groq -> OpenAI for structured JSON (InsightsIAS-style).
- If model missing fields, uses a deterministic offline summarizer to fill:
  context, about, facts_and_policies, policy_points, detailed_brief, impact_or_analysis.
- Builds a clean PDF with stable layout and emails it.
- Can run a single-URL test with --test-url.
"""

import os, re, io, sys, time, json, ssl, smtplib, datetime, requests, feedparser, argparse
from email.message import EmailMessage
from pprint import pprint

# PDF libs
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage,
    KeepTogether, ListFlowable, ListItem
)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT
from PIL import Image as PILImage, ImageDraw, ImageFont

# ---------------- Config ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "mixtral-8x7b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

PDF_FILENAME_TEMPLATE = "UPSC_AI_Brief_{date}.pdf"

RSS_FEEDS = [
    "https://www.insightsonindia.com/feed",
    "https://www.drishtiias.com/feed",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://prsindia.org/theprsblog/feed",
    "https://www.thehindu.com/news/national/feeder/default.rss",
]

MAX_CANDIDATES = 40
MAX_INCLUSIONS = 12

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

# ---------------- Utilities ----------------
def safe_trim(text, max_chars=3800):
    if not text: return ""
    if len(text) <= max_chars: return text
    window = text[:max_chars]
    last_p = max(window.rfind('.'), window.rfind('?'), window.rfind('!'))
    if last_p > int(max_chars*0.6):
        return window[:last_p+1]
    last_n = window.rfind('\n')
    if last_n > int(max_chars*0.5):
        return window[:last_n]
    return re.sub(r'\s+\S*?$','',window)

def clean_text(raw):
    if not raw: return ""
    t = raw.replace("\r", "\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading"]
    for p in junk: t = re.sub(p, " ", t, flags=re.I)
    lines = [ln.strip() for ln in t.splitlines() if len(ln.strip())>30 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    out = "\n\n".join(lines)
    out = re.sub(r'\n{3,}','\n\n', out)
    return out.strip()

def extract_json_substring(s):
    if not s: return None
    start = s.find('{')
    if start == -1: return None
    depth = 0
    for i in range(start, len(s)):
        if s[i]=='{': depth+=1
        elif s[i]=='}':
            depth-=1
            if depth==0:
                try:
                    return json.loads(s[start:i+1])
                except:
                    return None
    return None

def is_boilerplate(title, text):
    c = (title + " " + text).lower()
    patterns = ["upsc current affairs", "instalinks", "covers important current affairs", "gs paper", "content for mains enrichment"]
    return sum(1 for p in patterns if p in c) >= 2

def is_india_relevant(title, text, url):
    c = (title + " " + text).lower()
    if "india" in c or "indian" in c: return True
    if any(d in url for d in [".gov.in","insightsonindia","drishtiias","pib.gov.in","prsindia"]): return True
    allow = ["nobel","climate","un","summit","report","treaty","agreement","world bank","imf"]
    return any(a in c for a in allow)

# ---------------- Article extraction ----------------
def extract_article_text_and_image(url, timeout=12):
    headers = {"User-Agent":"Mozilla/5.0"}
    try:
        from newspaper import Article
        art = Article(url)
        art.download(); art.parse()
        text = clean_text(art.text or "")
        top_image = getattr(art,"top_image",None)
        img_bytes = None
        if top_image:
            try:
                r = requests.get(top_image, timeout=timeout, headers=headers)
                if r.status_code==200 and 'image' in r.headers.get('Content-Type',''):
                    img_bytes = r.content
            except:
                img_bytes = None
        if text and len(text.split())>50:
            return text, img_bytes
    except Exception:
        pass
    # readability fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        from readability import Document
        doc = Document(r.text)
        summary_html = doc.summary()
        text = clean_text(re.sub(r'<[^>]+>',' ', summary_html))
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if text and len(text.split())>50:
            return text, img_bytes
    except Exception:
        pass
    # simple html strip fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)',' ', r.text)
        stripped = re.sub(r'<[^>]+>',' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        if text and len(text.split())>50:
            return text, None
    except Exception:
        pass
    return "", None

# ---------------- Model calls ----------------
def call_groq(prompt, max_tokens=1200):
    if not GROQ_API_KEY: return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type":"application/json"}
    payload = {"model": GROQ_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("Groq error:", e)
    return None

def call_openai(prompt, max_tokens=1200):
    if not OPENAI_API_KEY: return None
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}
    payload = {"model": OPENAI_MODEL, "messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("OpenAI error:", e)
    return None

def summarize_via_model(title, url, text):
    trimmed = safe_trim(text, max_chars=3600)
    prompt = f"""
You are an expert UPSC analyst. Return STRICT valid JSON only in this format:

{{
  "include":"yes/no",
  "category":"GS1/GS2/GS3/GS4/CME/Mapping/FFP",
  "section_heading":"",
  "context":"",
  "about":"",
  "facts_and_policies": [],
  "policy_points": [],
  "detailed_brief":"",
  "impact_or_analysis": [],
  "upsc_relevance":""
}}

Guidelines:
- Put 3-6 factual bullets in facts_and_policies (schemes, ministries, dates, numbers).
- policy_points: list scheme/act/provision names if present.
- detailed_brief: 120-220 words synthesizing cause, facts, implications.
- If text truncated, set context to "SOURCE_ONLY".
- Do NOT invent facts.

Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    parsed = None
    if GROQ_API_KEY:
        parsed = call_groq(prompt)
        if parsed:
            print("[model] Groq produced parsed JSON (truncated):", str(parsed)[:500])
    if parsed is None and OPENAI_API_KEY:
        parsed = call_openai(prompt)
        if parsed:
            print("[model] OpenAI produced parsed JSON (truncated):", str(parsed)[:500])
    return parsed

# -------------- Offline deterministic summarizer (guaranteed) --------------
FACT_PATTERNS = [
    r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b\d{1,3}(?:,\d{3})+\b', r'\b(ICMR|ISRO|NITI Aayog|WHO|World Bank|UN|KAUST|IMF)\b',
    r'\b(Ministry of|Department of|Scheme|Policy|Act|Bill|Programme|Program)\b', r'\b(report|index|survey)\b'
]

def extract_sentences(text):
    sents = [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]
    return sents

def make_context(text):
    sents = extract_sentences(text)
    if not sents: return ""
    # first 1-2 sentences as context
    return " ".join(sents[:2])[:600]

def make_about(text):
    sents = extract_sentences(text)
    if len(sents) >= 4:
        about = " ".join(sents[1:4])
    elif len(sents) >=2:
        about = " ".join(sents[0:2])
    else:
        about = text[:400]
    return about

def extract_facts(text, max_bullets=6):
    sents = extract_sentences(text)
    scored=[]
    for s in sents:
        score = 0
        for pat in FACT_PATTERNS:
            if re.search(pat, s, flags=re.I):
                score += 1
        if score>0:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    bullets=[]
    seen=set()
    for _, s in scored:
        b = re.sub(r'\s+',' ', s)
        if len(b)>200: b=b[:200].rsplit(' ',1)[0]+'...'
        if b not in seen:
            bullets.append(b)
            seen.add(b)
        if len(bullets) >= max_bullets:
            break
    if not bullets:
        # fallback to numerical facts
        nums = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_bullets]:
            bullets.append(f"Figure: {n}")
    return bullets

def extract_policy_points(text, max_items=4):
    sents = extract_sentences(text)
    pts=[]
    for s in sents:
        if re.search(r'\b(Ministry|Department|Policy|Scheme|Act|Bill|NITI Aayog|Prime Minister|PM)\b', s, flags=re.I):
            p = re.sub(r'\s+',' ', s.strip())
            if len(p)>200: p = p[:200].rsplit(' ',1)[0] + '...'
            if p not in pts: pts.append(p)
        if len(pts) >= max_items: break
    return pts

def make_detailed_brief(title, about, facts, policy_points):
    # build 140-220 word paragraph by combining about + top facts + one policy mention
    parts = []
    if about:
        parts.append(about)
    parts.extend(facts[:3])
    if policy_points:
        parts.append(policy_points[0])
    para = " ".join(parts)
    if len(para) < 120:
        para = para + " " + (" ".join(facts[3:5])) if len(facts)>3 else para
    # ensure not too long
    return para[:1400]

def make_impact(text):
    # create 2-4 analytical bullets heuristically
    sents = extract_sentences(text)
    impacts = []
    for s in sents:
        if len(s) > 60 and any(w in s.lower() for w in ["impact","challenge","concern","affect","threat","benefit","important","key"]):
            impacts.append(s if len(s)<200 else s[:200]+"...")
        if len(impacts) >= 4:
            break
    # fallback generic analyses
    if not impacts:
        impacts = [
            "Significance for policy / governance or exam perspective.",
            "Potential implications for stakeholders and implementation.",
        ]
    return impacts

# ---------------- Final article processor ----------------
def process_article(title, url, text, img_bytes):
    # 1) try model
    parsed = summarize_via_model(title, url, text)
    used_model = False
    if parsed:
        used_model = True
    # 2) ensure fields present; if anything missing use offline deterministic logic
    if not parsed:
        parsed = {}
    # ensure include default yes
    parsed["include"] = str(parsed.get("include","yes"))
    parsed["section_heading"] = parsed.get("section_heading") or title
    # context/about
    if not parsed.get("context"):
        parsed["context"] = make_context(text)
    if not parsed.get("about"):
        parsed["about"] = make_about(text)
    # facts
    facts = parsed.get("facts_and_policies") or []
    if not facts or len([f for f in facts if len(f.strip())>8]) < 2:
        offline_facts = extract_facts(text, max_bullets=6)
        # merge (model facts first then offline)
        parsed["facts_and_policies"] = (facts + offline_facts)[:6]
    # policy points
    policy_points = parsed.get("policy_points") or []
    if not policy_points:
        parsed["policy_points"] = extract_policy_points(text, max_items=4)
    # sub_sections: keep model-provided + ensure key provisions present
    subs = parsed.get("sub_sections") or []
    if parsed.get("policy_points"):
        subs.append({"heading":"Key Provisions / Policy Mentions", "points": parsed["policy_points"]})
    parsed["sub_sections"] = subs
    # detailed_brief
    if not parsed.get("detailed_brief") or len((parsed.get("detailed_brief") or "").strip()) < 120:
        parsed["detailed_brief"] = make_detailed_brief(title, parsed.get("about",""), parsed["facts_and_policies"], parsed.get("policy_points",[]))
    # impact
    if not parsed.get("impact_or_analysis"):
        parsed["impact_or_analysis"] = make_impact(text)
    # category
    cat = parsed.get("category") or ""
    if cat:
        m = re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
        if m:
            parsed["category"] = f"GS{m.group(1)}"
        else:
            parsed["category"] = cat
    else:
        # rough scoring
        combined = (title + " " + text).lower()
        if any(k in combined for k in ["constitution","parliament","supreme court","policy","minister","government"]):
            parsed["category"] = "GS2"
        elif any(k in combined for k in ["economy","gdp","rbi","industry","inflation","trade","agriculture","isro","science","nobel","environment","climate"]):
            parsed["category"] = "GS3"
        elif any(k in combined for k in ["ethic","ethics","corruption","integrity"]):
            parsed["category"] = "GS4"
        elif any(k in combined for k in ["study","report","survey","analysis","index"]):
            parsed["category"] = "CME"
        else:
            parsed["category"] = "Misc"
    parsed["image_bytes"] = img_bytes
    parsed.setdefault("upsc_relevance", CATEGORY_LABELS.get(parsed["category"], parsed["category"]))
    parsed.setdefault("source", url)
    parsed.setdefault("include", "yes")
    parsed.setdefault("sub_sections", parsed.get("sub_sections", []))
    return parsed, used_model

# ---------------- PDF builder (stable) ----------------
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
        x=(size[0]-w)/2; y=(size[1]-h)/2
        draw.text((x,y), text, font=font, fill=fg)
        bio=io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
        return bio.read()
    except Exception as e:
        print("logo error", e); return None

def build_pdf(articles, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CardTitle", fontSize=12, leading=14))
    styles.add(ParagraphStyle(name="CardBody", fontSize=10, leading=13))
    styles.add(ParagraphStyle(name="SectionHeader", fontSize=13, leading=15, textColor=colors.HexColor("#1f4e79"), spaceBefore=8, spaceAfter=6))
    story=[]
    today_str = datetime.datetime.now().strftime("%d %B %Y")
    logo_bytes = generate_logo_bytes()
    left_elem = RLImage(io.BytesIO(logo_bytes), width=110, height=28) if logo_bytes else Paragraph("DailyCAThroughAI", styles["CardBody"])
    right_elem = Paragraph(f"<b>UPSC CURRENT AFFAIRS</b><br/>{today_str}", styles["CardTitle"])
    header_table = Table([[left_elem, right_elem]], colWidths=[120, doc.width-120])
    header_table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#f4f8fb")), ("LEFTPADDING",(0,0),(-1,-1),8)]))
    story.append(header_table); story.append(Spacer(1,8))

    # group by categories in order
    order = ["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    grouped = {k:[] for k in order}
    for a in articles:
        cat = a.get("category","Misc")
        if cat not in grouped: grouped["Misc"].append(a)
        else: grouped[cat].append(a)

    for cat in order:
        items = grouped.get(cat, [])
        if not items: continue
        story.append(Paragraph(CATEGORY_LABELS.get(cat, cat), styles["SectionHeader"]))
        story.append(Spacer(1,6))
        for it in items:
            # left block (flowables)
            left = []
            left.append(Paragraph(f"<b>{it.get('section_heading','Untitled')}</b>", styles["CardTitle"]))
            if it.get("context"):
                left.append(Paragraph(f"<b>Context:</b> {it.get('context')}", styles["CardBody"]))
            if it.get("about"):
                left.append(Paragraph(f"<b>About:</b> {it.get('about')}", styles["CardBody"]))
            facts = it.get("facts_and_policies",[]) or []
            if facts:
                left.append(Paragraph("<b>Facts & Data:</b>", styles["CardBody"]))
                bullets = [ListItem(Paragraph(f, styles["CardBody"]), leftIndent=6) for f in facts]
                left.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))
            subs = it.get("sub_sections",[]) or []
            for s in subs:
                head = s.get("heading",""); pts = s.get("points",[]) or []
                if head:
                    left.append(Spacer(1,4)); left.append(Paragraph(f"<b>{head}:</b>", styles["CardBody"]))
                if pts:
                    bullets=[ListItem(Paragraph(p, styles["CardBody"]), leftIndent=6) for p in pts]
                    left.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))
            if it.get("detailed_brief"):
                left.append(Spacer(1,4)); left.append(Paragraph(f"<b>Detailed Brief:</b> {it.get('detailed_brief')}", styles["CardBody"]))
            impact = it.get("impact_or_analysis",[]) or []
            if impact:
                left.append(Spacer(1,4)); left.append(Paragraph("<b>Impact / Analysis:</b>", styles["CardBody"]))
                bullets = [ListItem(Paragraph(p, styles["CardBody"]), leftIndent=6) for p in impact]
                left.append(ListFlowable(bullets, bulletType='bullet', leftIndent=8))

            left_block = KeepTogether(left)

            # right image
            img_elem = None
            im_bytes = it.get("image_bytes")
            if im_bytes:
                try:
                    pil = PILImage.open(io.BytesIO(im_bytes)); pil.thumbnail((150,110))
                    bb = io.BytesIO(); pil.save(bb, format="PNG"); bb.seek(0)
                    img_elem = RLImage(bb, width=min(140,pil.width), height=min(100,pil.height))
                except:
                    img_elem = None
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
                story.append(left_block)
            story.append(Spacer(1,10))

    # footer note
    note_style = ParagraphStyle("note", fontSize=8, textColor=colors.grey, alignment=TA_LEFT)
    story.append(Paragraph("Note: Auto-generated summaries; verify facts from original sources if needed.", note_style))

    try:
        doc.build(story)
        return out_path if (out_path:=out_path) else out_path
    except Exception as e:
        print("PDF build error:", e)
        # minimal fallback
        try:
            from reportlab.pdfgen import canvas
            c = canvas.Canvas(out_path, pagesize=A4)
            c.setFont("Helvetica-Bold",12); c.drawString(50,800,"UPSC Daily Brief (Partial)")
            c.setFont("Helvetica",10); c.drawString(50,780,"Some items skipped due to layout issues.")
            c.save()
            return out_path
        except Exception as e2:
            print("Fallback PDF failed", e2)
            return None

# ---------------- Email ----------------
def email_pdf_file(path):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("Set SMTP_USER / SMTP_PASSWORD / EMAIL_TO env variables")
    msg = EmailMessage()
    msg["Subject"] = f"UPSC AI Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg["From"] = SMTP_USER; msg["To"] = EMAIL_TO
    msg.set_content("Attached: UPSC AI Current Affairs Brief (auto-generated).")
    with open(path,"rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(path))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------------- Main ----------------
def main(test_url=None):
    date_str = datetime.date.today().isoformat()
    output_pdf = PDF_FILENAME_TEMPLATE.format(date=date_str if not test_url else "TEST")
    candidates=[]
    if test_url:
        candidates.append({"title":"Test article", "link":test_url})
    else:
        for feed in RSS_FEEDS:
            try:
                f = feedparser.parse(feed)
                for e in f.entries[:12]:
                    title = e.get("title",""); link = e.get("link","")
                    if title and link:
                        candidates.append({"title":title, "link":link})
                    if len(candidates) >= MAX_CANDIDATES: break
            except Exception as ex:
                print("Feed error", feed, ex)
    print("Candidates:", len(candidates))

    processed=[]
    included=0; seen=set()
    for c in candidates:
        if included >= MAX_INCLUSIONS: break
        title = c["title"].strip(); link = c["link"]
        if link in seen: continue
        seen.add(link)
        print("Processing:", title)
        text, img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text, skip"); continue
        if is_boilerplate(title, text):
            print(" -> boilerplate skipped"); continue
        if not is_india_relevant(title, text, link):
            print(" -> not India-relevant skipped"); continue

        parsed, used_model = process_article(title, link, text, img)
        print(f" -> included: category={parsed.get('category')} (model_used={used_model})")
        processed.append(parsed)
        included += 1
        time.sleep(0.7)

    if not processed:
        print("No relevant items found. Exiting.")
        return

    pdf_path = build_pdf(processed, output_pdf)
    if not pdf_path:
        print("PDF generation failed.")
        return
    print("PDF created:", pdf_path)

    if not test_url:
        try:
            email_pdf_file(pdf_path)
        except Exception as e:
            print("Email failed:", e)
    else:
        print("Test mode: not emailing. Open", pdf_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Run script for a single test URL and produce TEST PDF", default=None)
    args = parser.parse_args()
    main(test_url=args.test_url)