#!/usr/bin/env python3
"""
generate_and_send.py — filtered to avoid question/answer & boilerplate posts.

Main fixes:
- BLACKLIST_DOMAINS: default blocks insightsonindia.com (you can edit).
- is_question_article(): heuristics to skip Mains practice / Q&A / tutorial items.
- improved is_boilerplate() patterns.
- prints skip reasons in logs for debugging.
"""

import os, re, io, sys, time, json, ssl, argparse, smtplib, datetime, requests, feedparser
from email.message import EmailMessage
from pprint import pprint

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, ListFlowable, ListItem, HRFlowable
)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT
from PIL import Image as PILImage, ImageDraw, ImageFont

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "mixtral-8x7b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
BING_API_KEY = os.environ.get("BING_API_KEY")

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

# Domains to always skip (you said Insight was only for reference)
BLACKLIST_DOMAINS = [
    "insightsonindia.com",
    # add more domains here if you want to exclude them
]

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
    w = text[:max_chars]
    last = max(w.rfind('.'), w.rfind('?'), w.rfind('!'))
    if last > int(max_chars*0.6): return w[:last+1]
    ln = w.rfind('\n')
    if ln > int(max_chars*0.5): return w[:ln]
    return re.sub(r'\s+\S*?$', '', w)

def clean_text(raw):
    if not raw: return ""
    t = raw.replace("\r","\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading", r"FOLLOW US", r"Download PDF"]
    for p in junk: t = re.sub(p, " ", t, flags=re.I)
    lines = [ln.strip() for ln in t.splitlines() if len(ln.strip())>30 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    out = "\n\n".join(lines)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()

def extract_json_substring(s):
    if not s: return None
    i = s.find('{'); 
    if i==-1: return None
    depth=0
    for j in range(i,len(s)):
        if s[j]=='{': depth+=1
        elif s[j]=='}':
            depth-=1
            if depth==0:
                try: return json.loads(s[i:j+1])
                except: return None
    return None

def domain_from_url(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return h.lower()
    except:
        return ""

# ------------- New: detect question/answer / mains practice content -------------
QUESTION_KEYWORDS = [
    "mains", "answer writing", "answer", "question", "key demand", "instalinks",
    "secure", "mains practice", "answer must", "answer should", "insta links",
    "model answer", "practice question", "mains answer", "questions and answers"
]
QUESTION_MARKERS = [r'\bQ[0-9]\b', r'\bQ1\b', r'\bQ2\b', r'\bQ3\b', r'\bQ4\b']

def is_question_article(title, text):
    """Return True if article looks like a Q/A or mains practice piece (skip it)."""
    t = (title or "").lower()
    b = (text or "").lower()
    # title based heuristics
    for k in QUESTION_KEYWORDS:
        if k in t:
            return True
    # body-based heuristics: presence of "Key Demand", "Model Answer", "Q1/Q2", "Answer must", etc
    if re.search(r'key demand of the question', b):
        return True
    if re.search(r'answer must|model answer|model answers|marking scheme|how to answer', b):
        return True
    for pat in QUESTION_MARKERS:
        if re.search(pat, b):
            return True
    # if the first section is "UPSC Mains" or "Mains Enrichment" — treat as question bank
    if re.search(r'\b(upsc mains|mains enrichment|mains answer|mains practice)\b', b):
        return True
    # if too many short directive sentences (e.g., "Do this", "Discuss", "Explain") it's likely Q/A
    directives = re.findall(r'\b(discuss|explain|analyse|analyse|critically|comment on|what are)\b', b)
    if len(directives) >= 3 and len(b.split()) < 800:
        return True
    return False

def is_boilerplate(title, text):
    c = (title + " " + (text or "")).lower()
    patterns = [
        "upsc current affairs", "instalinks", "insta links", "covers important current affairs",
        "gs paper", "content for mains enrichment", "subscribe", "answer writing"
    ]
    return sum(1 for p in patterns if p in c) >= 2

def is_india_relevant(title, text, url):
    c = (title + " " + (text or "")).lower()
    if "india" in c or "indian" in c: return True
    if any(d in url for d in [".gov.in","drishtiias","insightsonindia","pib.gov.in","prsindia"]): return True
    allow = ["nobel","climate","un","summit","report","treaty","agreement","world bank","imf"]
    return any(a in c for a in allow)

# ---------------- Article extraction ----------------
def extract_article_text_and_image(url, timeout=12):
    headers={"User-Agent":"Mozilla/5.0"}
    try:
        from newspaper import Article
        art=Article(url); art.download(); art.parse()
        text = clean_text(art.text or "")
        top_image = getattr(art,"top_image",None)
        img_bytes=None
        if top_image:
            try:
                r=requests.get(top_image, timeout=timeout, headers=headers)
                if r.status_code==200 and 'image' in r.headers.get('Content-Type',''):
                    img_bytes=r.content
            except: img_bytes=None
        if text and len(text.split())>50: return text, img_bytes
    except Exception:
        pass
    try:
        r=requests.get(url, timeout=timeout, headers=headers)
        from readability import Document
        doc = Document(r.text)
        summary_html = doc.summary()
        text = clean_text(re.sub(r'<[^>]+>',' ', summary_html))
        img_bytes=None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2=requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes=r2.content
            except: img_bytes=None
        if text and len(text.split())>50: return text, img_bytes
    except Exception:
        pass
    try:
        r=requests.get(url, timeout=timeout, headers=headers)
        html=re.sub(r'(?is)<(script|style).*?>.*?(</\1>)',' ', r.text)
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        if text and len(text.split())>50: return text, None
    except Exception:
        pass
    return "", None

# ---------------- Model / offline summariser and web enrichment (same as before) ----------------
# For brevity in this reply I keep the same model/offline/web functions previously used:
# - call_groq(), call_openai(), summarize_via_model()
# - offline extractors: extract_sentences(), make_context(), make_about(), extract_facts(), extract_policy_points(), make_detailed_brief(), make_impact()
# - web_enrich() using serpapi_search() and bing_search()
#
# (In your repo, keep the earlier implementations of those functions — I assume you already have them.)
# For completeness below I include the essential offline extraction functions used in filtering and PDF.

FACT_PATTERNS = [r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b(Ministry|Scheme|Policy|Act|Report|NITI Aayog)\b']
def split_sentences(text): return [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]
def make_context(text):
    s = split_sentences(text)
    return " ".join(s[:2]) if s else ""
def make_about(text):
    s = split_sentences(text)
    if len(s) >= 4: return " ".join(s[1:4])
    if len(s) >= 2: return " ".join(s[0:2])
    return text[:400]
def extract_facts(text, max_b=6):
    s = split_sentences(text); scored=[]
    for sent in s:
        sc=0
        for pat in FACT_PATTERNS:
            if re.search(pat, sent, flags=re.I): sc+=1
        if sc>0: scored.append((sc,sent))
    scored.sort(key=lambda x:x[0], reverse=True)
    bullets=[]; used=set()
    for _,sent in scored:
        b=re.sub(r'\s+',' ', sent).strip()
        if len(b)>200: b=b[:200].rsplit(' ',1)[0]+'...'
        if b not in used: bullets.append(b); used.add(b)
        if len(bullets)>=max_b: break
    if not bullets:
        nums=re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_b]: bullets.append(f"Figure: {n}")
    return bullets
def extract_policy_points(text, max_n=4):
    s=split_sentences(text); pts=[]
    for sent in s:
        if re.search(r'\b(Ministry|Department|Scheme|Policy|Act|Bill|NITI Aayog|Prime Minister)\b', sent, flags=re.I):
            p=re.sub(r'\s+',' ', sent).strip()
            if len(p)>200: p=p[:200].rsplit(' ',1)[0]+'...'
            if p not in pts: pts.append(p)
        if len(pts)>=max_n: break
    return pts
def make_detailed_brief(title, about, facts, policies):
    parts=[about]+facts[:3]+(policies[:1] if policies else [])
    para=" ".join([p for p in parts if p])
    if len(para.split()) < 80:
        para = para + " " + (" ".join(facts[3:5])) if len(facts)>3 else para
    return para[:1400]
def make_impact(text):
    s=split_sentences(text); im=[]
    for sent in s:
        if len(sent)>60 and any(w in sent.lower() for w in ["impact","concern","affect","challenge","benefit","important"]):
            im.append(sent if len(sent)<200 else sent[:200]+"...")
        if len(im)>=4: break
    if not im: im=["Significance for policy and governance.","Potential implications for stakeholders and implementation."]
    return im

# ---------------- PDF builder (keeps sources hidden; relevance shown at header) ----------------
def ensure_style(stylesheet, name, **kwargs):
    if name in stylesheet.byName: return stylesheet.byName[name]
    ps = ParagraphStyle(name=name, **kwargs)
    try:
        stylesheet.add(ps)
    except Exception:
        stylesheet.byName[name] = ps
    return ps

def generate_logo_bytes(text="DailyCAThroughAI", size=(420,80), bgcolor=(31,78,121), fg=(255,255,255)):
    try:
        img = PILImage.new("RGB", size, bgcolor)
        draw = ImageDraw.Draw(img)
        try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        except: font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0,0), text, font=font); w=bbox[2]-bbox[0]; h=bbox[3]-bbox[1]
        except:
            w,h = draw.textsize(text, font=font)
        x=(size[0]-w)/2; y=(size[1]-h)/2
        draw.text((x,y), text, font=font, fill=fg)
        bio=io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
        return bio.read()
    except Exception as e:
        print("logo error", e); return None

def make_image_element_simple(im_bytes, max_w=180, max_h=120):
    if not im_bytes: return None
    try:
        pil = PILImage.open(io.BytesIO(im_bytes)); pil.load()
        w,h = pil.size
        if w<=0 or h<=0 or w>6000 or h>6000: return None
        ratio = min(max_w/float(w), max_h/float(h), 1.0)
        new_w, new_h = int(w*ratio), int(h*ratio)
        pil = pil.resize((new_w,new_h), PILImage.LANCZOS)
        bb=io.BytesIO(); pil.save(bb, format='PNG'); bb.seek(0)
        img = RLImage(bb, width=new_w, height=new_h)
        img.hAlign='RIGHT'
        return img
    except Exception as e:
        print("make_image_element_simple skipped image:", e)
        return None

def split_into_paragraphs(text, chunk=800):
    if not text: return []
    text=text.strip()
    if len(text)<=chunk: return [text]
    parts=[]
    start=0; L=len(text)
    while start < L:
        end=start+chunk
        if end < L:
            next_break = text.rfind('.', start, end)
            if next_break <= start: next_break = text.rfind(' ', start, end)
            if next_break <= start: next_break = end
            end = next_break
        parts.append(text[start:end].strip())
        start = end if end>start else start+chunk
    return parts

def build_pdf_simple(articles, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    ensure_style(styles, "UPSC_Title", fontSize=13, leading=15)
    ensure_style(styles, "UPSC_Body", fontSize=10, leading=13)
    ensure_style(styles, "UPSC_Section", fontSize=12, leading=14, textColor=colors.HexColor("#1f4e79"))
    ensure_style(styles, "UPSC_SmallGray", fontSize=8, leading=10, textColor=colors.grey)

    story=[]
    today = datetime.datetime.now().strftime("%d %B %Y")
    logo = generate_logo_bytes()
    if logo:
        img = RLImage(io.BytesIO(logo), width=120, height=32); img.hAlign='LEFT'; story.append(img)
    story.append(Paragraph(f"<b>UPSC CURRENT AFFAIRS</b> — {today}", styles["UPSC_Title"]))
    story.append(Spacer(1,8))

    order=["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    grouped={k:[] for k in order}
    for a in articles:
        cat=a.get("category","Misc"); grouped.setdefault(cat,[]).append(a)

    for cat in order:
        items = grouped.get(cat,[])
        if not items: continue
        story.append(Paragraph(CATEGORY_LABELS.get(cat,cat), styles["UPSC_Section"])); story.append(Spacer(1,6))
        for it in items:
            # show relevance at header (not at bottom)
            story.append(Paragraph(f"<b>[{it.get('category','')}] {it.get('section_heading','Untitled')}</b>", styles["UPSC_Title"]))
            story.append(Spacer(1,4))
            img_elem = None
            if it.get("image_bytes"): img_elem = make_image_element_simple(it.get("image_bytes"))
            if img_elem:
                story.append(img_elem); story.append(Spacer(1,4))

            if it.get("context"):
                for p in split_into_paragraphs(it.get("context","")):
                    story.append(Paragraph(f"<b>Context:</b> {p}", styles["UPSC_Body"]))
            if it.get("about"):
                for p in split_into_paragraphs(it.get("about","")):
                    story.append(Paragraph(f"<b>About:</b> {p}", styles["UPSC_Body"]))

            facts = it.get("facts_and_policies",[]) or []
            if facts:
                story.append(Paragraph("<b>Facts & Data:</b>", styles["UPSC_Body"]))
                bullets=[ListItem(Paragraph(f, styles["UPSC_Body"])) for f in facts]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            subs = it.get("sub_sections",[]) or []
            for s in subs:
                head = s.get("heading",""); pts = s.get("points",[]) or []
                if head:
                    story.append(Paragraph(f"<b>{head}:</b>", styles["UPSC_Body"]))
                if pts:
                    bullets=[ListItem(Paragraph(p, styles["UPSC_Body"])) for p in pts]
                    story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            if it.get("detailed_brief"):
                story.append(Paragraph("<b>Detailed Brief:</b>", styles["UPSC_Body"]))
                for p in split_into_paragraphs(it.get("detailed_brief","")):
                    story.append(Paragraph(p, styles["UPSC_Body"]))

            impact = it.get("impact_or_analysis",[]) or []
            if impact:
                story.append(Paragraph("<b>Impact / Analysis:</b>", styles["UPSC_Body"]))
                bullets=[ListItem(Paragraph(p, styles["UPSC_Body"])) for p in impact]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            story.append(Spacer(1,8))
            hr = HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cfdff0"))
            story.append(hr); story.append(Spacer(1,8))

    story.append(Paragraph("Note: Auto-generated summaries; verify facts from original sources if needed.", ParagraphStyle(name="note", fontSize=8, textColor=colors.grey)))
    try:
        doc.build(story); return out_path
    except Exception as e:
        print("PDF build failed:", e); return None

# ---------------- Main pipeline: fetch -> filter -> process -> pdf -> email ----------------
def main(test_url=None):
    date_str = datetime.date.today().isoformat()
    output_pdf = PDF_FILENAME_TEMPLATE.format(date=(date_str if not test_url else "TEST"))
    candidates=[]

    if test_url:
        candidates.append({"title":"Test article","link":test_url})
    else:
        for feed in RSS_FEEDS:
            try:
                f = feedparser.parse(feed)
                for e in f.entries[:12]:
                    title=e.get("title",""); link=e.get("link","")
                    if title and link: candidates.append({"title":title,"link":link})
                    if len(candidates) >= MAX_CANDIDATES: break
            except Exception as ex:
                print("Feed error", feed, ex)
    print("Candidates:", len(candidates))

    processed=[]; included=0; seen=set()
    for c in candidates:
        if included >= MAX_INCLUSIONS: break
        title=c["title"].strip(); link=c["link"]
        if link in seen: continue
        seen.add(link)
        dom = domain_from_url(link)
        if any(b in dom for b in BLACKLIST_DOMAINS):
            print("Skipping (blacklisted domain):", dom, title); continue
        print("Processing:", title)
        text,img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text, skip"); continue
        if is_boilerplate(title, text):
            print(" -> skipped boilerplate:", title); continue
        if is_question_article(title, text):
            print(" -> skipped Q/A / Mains practice article:", title); continue
        if not is_india_relevant(title, text, link):
            print(" -> not India-relevant skip"); continue

        # Use your existing process_article (model + offline + optional web enrichment)
        parsed, used_model = process_article(title, link, text, img)  # assume implemented earlier in your repo
        if str(parsed.get("include","yes")).lower() != "yes":
            print(" -> model marked not relevant; skipping"); continue

        print(f" -> included: category={parsed.get('category')} (model_used={used_model})")
        processed.append(parsed); included += 1
        time.sleep(0.6)

    if not processed:
        print("No relevant items found. Exiting.")
        return

    pdf_path = build_pdf_simple(processed, output_pdf)
    if not pdf_path:
        print("PDF generation failed."); return
    print("PDF created:", pdf_path)

    if test_url:
        print("Test mode — not emailing. Inspect", pdf_path)
    else:
        try:
            # email only if SMTP info is configured
            if SMTP_USER and SMTP_PASSWORD and EMAIL_TO:
                # attach file and send
                msg = EmailMessage()
                msg["Subject"] = f"UPSC AI Brief — {datetime.date.today().strftime('%d %b %Y')}"
                msg["From"] = SMTP_USER; msg["To"] = EMAIL_TO
                msg.set_content("Attached: UPSC AI Current Affairs Brief (auto-generated).")
                with open(pdf_path, "rb") as f: msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))
                ctx = ssl.create_default_context()
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                    s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
                print("Email sent to", EMAIL_TO)
            else:
                print("SMTP not configured -> skipping email (PDF generated only).")
        except Exception as e:
            print("Email failed:", e)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Run script for a single test URL and produce TEST PDF", default=None)
    args = parser.parse_args()
    main(test_url=args.test_url)