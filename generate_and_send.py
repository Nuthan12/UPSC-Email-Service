#!/usr/bin/env python3
"""
generate_and_send.py — simplified, robust layout (no fragile tables)

Goals:
- Prevent skipped items due to layout errors.
- Produce boxed article cards consistently (title, small right-aligned image,
  Context, About, Facts, PolicyPoints, Detailed Brief, Impact).
- Keep paragraphs small so ReportLab can paginate them.
- Use previous model/offline/web enrichment pipeline unchanged.
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

# ---------------- Config (same as before) ----------------
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

# ---------------- Utilities (kept simple) ----------------
def safe_trim(text, max_chars=3800):
    if not text: return ""
    if len(text) <= max_chars: return text
    w = text[:max_chars]
    last = max(w.rfind('.'), w.rfind('?'), w.rfind('!'))
    if last > int(max_chars*0.6): return w[:last+1]
    ln = w.rfind('\n'); 
    if ln > int(max_chars*0.5): return w[:ln]
    return re.sub(r'\s+\S*?$', '', w)

def clean_text(raw):
    if not raw: return ""
    t = raw.replace("\r","\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading"]
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
                try:
                    return json.loads(s[i:j+1])
                except:
                    return None
    return None

# ---------------- Article extraction (unchanged) ----------------
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
        stripped = re.sub(r'<[^>]+>',' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        if text and len(text.split())>50: return text, None
    except Exception:
        pass
    return "", None

# ---------------- Model + offline summarization (kept as in prior file) ----------------
def call_groq(prompt, max_tokens=1200):
    if not GROQ_API_KEY: return None
    try:
        url="https://api.groq.com/openai/v1/chat/completions"
        headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"}
        payload={"model":GROQ_MODEL,"messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
        r=requests.post(url,json=payload,headers=headers,timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("Groq error:", e)
    return None

def call_openai(prompt, max_tokens=1200):
    if not OPENAI_API_KEY: return None
    try:
        url="https://api.openai.com/v1/chat/completions"
        headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"}
        payload={"model":OPENAI_MODEL,"messages":[{"role":"user","content":prompt}], "temperature":0.0, "max_tokens":max_tokens}
        r=requests.post(url,json=payload,headers=headers,timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("OpenAI error:", e)
    return None

def summarize_via_model(title, url, text):
    trimmed = safe_trim(text, max_chars=3600)
    prompt = f"""
You are an expert UPSC analyst. Return STRICT valid JSON in this format:
{{"include":"yes/no","category":"GS1/GS2/GS3/GS4/CME/Mapping/FFP","section_heading":"","context":"","about":"","facts_and_policies":[],"policy_points":[],"detailed_brief":"","impact_or_analysis":[],"upsc_relevance":""}}
Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    parsed=None
    if GROQ_API_KEY:
        parsed = call_groq(prompt)
        if parsed: print("[model] Groq parsed (trunc):", str(parsed)[:300])
    if parsed is None and OPENAI_API_KEY:
        parsed = call_openai(prompt)
        if parsed: print("[model] OpenAI parsed (trunc):", str(parsed)[:300])
    return parsed

# Offline extractors (shortened versions)
FACT_PATTERNS = [r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b(Ministry|Scheme|Policy|Act|Report|NITI Aayog)\b']
def split_sentences(text): return [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]
def make_context(text):
    s=split_sentences(text); return " ".join(s[:2]) if s else ""
def make_about(text):
    s=split_sentences(text)
    if len(s)>=4: return " ".join(s[1:4])
    if len(s)>=2: return " ".join(s[0:2])
    return text[:400]
def extract_facts(text, max_b=6):
    s=split_sentences(text); scored=[]
    for sent in s:
        sc=0
        for pat in FACT_PATTERNS:
            if re.search(pat, sent, flags=re.I): sc+=1
        if sc>0: scored.append((sc,sent))
    scored.sort(key=lambda x:x[0], reverse=True)
    bullets=[]
    used=set()
    for _,sent in scored:
        b=re.sub(r'\s+',' ',sent)
        if len(b)>200: b=b[:200].rsplit(' ',1)[0]+'...'
        if b not in used:
            bullets.append(b); used.add(b)
        if len(bullets)>=max_b: break
    if not bullets:
        nums=re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_b]: bullets.append(f"Figure: {n}")
    return bullets
def extract_policy_points(text, max_n=4):
    s=split_sentences(text); pts=[]
    for sent in s:
        if re.search(r'\b(Ministry|Department|Scheme|Policy|Act|Bill|NITI Aayog|Prime Minister)\b', sent, flags=re.I):
            p=re.sub(r'\s+',' ',sent).strip()
            if len(p)>200: p=p[:200].rsplit(' ',1)[0]+'...'
            if p not in pts: pts.append(p)
        if len(pts)>=max_n: break
    return pts
def make_detailed_brief(title, about, facts, policies):
    parts=[about]+facts[:3]+(policies[:1] if policies else [])
    para=" ".join([p for p in parts if p])
    if len(para)<120: para = para + " " + " ".join(facts[3:5]) if len(facts)>3 else para
    return para[:1200]
def make_impact(text):
    s=split_sentences(text); im=[]
    for sent in s:
        if len(sent)>60 and any(w in sent.lower() for w in ["impact","concern","affect","challenge","benefit","important"]):
            im.append(sent if len(sent)<200 else sent[:200]+"...")
        if len(im)>=4: break
    if not im: im=["Significance for policy and governance.","Potential implications for stakeholders and implementation."]
    return im

# ---------------- Web enrichment (if keys present) ----------------
def serpapi_search(q,num=3):
    if not SERPAPI_KEY: return []
    try:
        r=requests.get("https://serpapi.com/search.json", params={"q":q,"api_key":SERPAPI_KEY,"num":num}, timeout=12)
        if r.status_code==200:
            js=r.json()
            res=[]
            for it in js.get("organic_results",[])[:num]:
                res.append({"title":it.get("title"), "link": it.get("link"), "snippet": it.get("snippet")})
            return res
    except Exception as e:
        print("SerpAPI error:", e)
    return []
def bing_search(q,num=3):
    if not BING_API_KEY: return []
    try:
        r=requests.get("https://api.bing.microsoft.com/v7.0/search", headers={"Ocp-Apim-Subscription-Key":BING_API_KEY}, params={"q":q,"count":num}, timeout=12)
        if r.status_code==200:
            js=r.json(); res=[]
            for it in js.get("webPages", {}).get("value", [])[:num]:
                res.append({"title":it.get("name"), "link":it.get("url"), "snippet":it.get("snippet")})
            return res
    except Exception as e:
        print("Bing error:", e)
    return []

def fetch_text_for_url(url):
    headers={"User-Agent":"Mozilla/5.0"}
    try:
        r=requests.get(url, timeout=12, headers=headers)
        if r.status_code!=200: return ""
        from readability import Document
        doc=Document(r.text)
        summary=doc.summary()
        txt=re.sub(r'<[^>]+>',' ', summary)
        return clean_text(txt)
    except Exception:
        try:
            r=requests.get(url, timeout=12)
            return clean_text(re.sub(r'<[^>]+>',' ', r.text))
        except:
            return ""

def web_enrich(title, text):
    q = title
    if re.search(r'\byojana\b|\bscheme\b|\bpradhan\b|\bmission\b', title, flags=re.I):
        q = title + " scheme details government website"
    results = serpapi_search(q, num=3) if SERPAPI_KEY else []
    if not results and BING_API_KEY:
        results = bing_search(q, num=3)
    web_facts=[]; web_policies=[]; sources=[]
    for r in results:
        url=r.get("link"); sources.append(url)
        ptext=fetch_text_for_url(url)
        if not ptext: continue
        wf = extract_facts(ptext, max_b=6)
        wp = extract_policy_points(ptext, max_n=6)
        for f in wf:
            if f not in web_facts: web_facts.append(f)
        for p in wp:
            if p not in web_policies: web_policies.append(p)
    return {"web_facts": web_facts, "web_policies": web_policies, "sources": sources}

# ---------------- Compose final parsed article (model + offline + web) ----------------
def process_article(title, url, text, img_bytes):
    parsed = summarize_via_model(title, url, text)
    used_model = parsed is not None
    if not parsed: parsed={}
    parsed["include"]=str(parsed.get("include","yes"))
    parsed["section_heading"]=parsed.get("section_heading") or title
    if not parsed.get("context"): parsed["context"]=make_context(text)
    if not parsed.get("about"): parsed["about"]=make_about(text)
    facts = parsed.get("facts_and_policies") or []
    if not facts or len([f for f in facts if len(f.strip())>8])<2:
        facts = facts + extract_facts(text, max_b=6)
    parsed["facts_and_policies"] = facts[:8]
    policy_points = parsed.get("policy_points") or []
    if not policy_points:
        policy_points = extract_policy_points(text, max_n=4)
    parsed["policy_points"] = policy_points
    subs = parsed.get("sub_sections") or []
    if parsed.get("policy_points"):
        subs.append({"heading":"Key Provisions / Policy Mentions","points": parsed["policy_points"]})
    parsed["sub_sections"]=subs
    if not parsed.get("detailed_brief") or len(parsed.get("detailed_brief","").strip())<120:
        parsed["detailed_brief"]=make_detailed_brief(title, parsed.get("about",""), parsed.get("facts_and_policies",[]), parsed.get("policy_points",[]))
    if not parsed.get("impact_or_analysis"):
        parsed["impact_or_analysis"]=make_impact(text)

    # category heuristic if not provided
    cat = parsed.get("category") or ""
    if cat:
        m=re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
        if m: parsed["category"]=f"GS{m.group(1)}"
    else:
        ctext=(title+" "+text).lower()
        if any(k in ctext for k in ["constitution","parliament","supreme court","policy","minister","government"]): parsed["category"]="GS2"
        elif any(k in ctext for k in ["economy","gdp","rbi","inflation","industry","agriculture","isro","science","environment","climate","nobel"]): parsed["category"]="GS3"
        elif any(k in ctext for k in ["ethic","ethics","corruption","integrity"]): parsed["category"]="GS4"
        elif any(k in ctext for k in ["study","report","survey","index","analysis"]): parsed["category"]="CME"
        else: parsed["category"]="Misc"

    # web enrichment trigger: scheme/yojana or insufficient facts/policies
    need_web = False
    if len(parsed.get("facts_and_policies",[]))<3 or len(parsed.get("policy_points",[]))<1:
        need_web = True
    if re.search(r'\byojana\b|\bscheme\b|\bpradhan\b|\bmission\b', title, flags=re.I):
        need_web = True

    if need_web and (SERPAPI_KEY or BING_API_KEY):
        try:
            enr = web_enrich(title, text)
            wf = enr.get("web_facts",[])[:6]
            wp = enr.get("web_policies",[])[:6]
            # merge unique
            merged_facts = parsed.get("facts_and_policies",[]) + [f for f in wf if f not in parsed.get("facts_and_policies",[])]
            merged_pols = parsed.get("policy_points",[]) + [p for p in wp if p not in parsed.get("policy_points",[])]
            parsed["facts_and_policies"] = merged_facts[:8]
            parsed["policy_points"] = merged_pols[:8]
            if parsed.get("policy_points"):
                parsed["sub_sections"].append({"heading":"Key Provisions / Policy Mentions (web-enriched)","points": parsed["policy_points"]})
            parsed["web_sources"] = enr.get("sources",[])
            print(" -> web enrichment used; sources:", parsed.get("web_sources",[])[:3])
        except Exception as e:
            print("Web enrichment failure:", e)

    parsed["image_bytes"] = img_bytes
    parsed.setdefault("upsc_relevance", CATEGORY_LABELS.get(parsed["category"], parsed["category"]))
    parsed.setdefault("source", url)
    parsed.setdefault("include","yes")
    return parsed, used_model

# ---------------- PDF builder — simplified, robust (no fragile tables) ----------------
def generate_logo_bytes(text="DailyCAThroughAI", size=(420,80), bgcolor=(31,78,121), fg=(255,255,255)):
    try:
        img = PILImage.new("RGB", size, bgcolor)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        except:
            font = ImageFont.load_default()
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
    text = text.strip()
    if not text: return []
    if len(text) <= chunk:
        return [text]
    parts = []
    start = 0
    while start < len(text):
        end = start + chunk
        # avoid cutting words
        if end < len(text):
            end = text.rfind(' ', start, end)
            if end <= start: end = start + chunk
        parts.append(text[start:end].strip())
        start = end
    return parts

def build_pdf_simple(articles, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Title", fontSize=13, leading=15))
    styles.add(ParagraphStyle(name="Body", fontSize=10, leading=13))
    styles.add(ParagraphStyle(name="Section", fontSize=12, leading=14, textColor=colors.HexColor("#1f4e79")))
    styles.add(ParagraphStyle(name="SmallGray", fontSize=8, leading=10, textColor=colors.grey))

    story=[]
    # header
    today = datetime.datetime.now().strftime("%d %B %Y")
    logo = generate_logo_bytes()
    if logo:
        img = RLImage(io.BytesIO(logo), width=120, height=32)
        img.hAlign='LEFT'
        story.append(img)
    story.append(Paragraph(f"<b>UPSC CURRENT AFFAIRS</b> — {today}", styles["Title"]))
    story.append(Spacer(1,8))

    # group by category
    order = ["GS1","GS2","GS3","GS4","CME","FFP","Mapping","Misc"]
    grouped={k:[] for k in order}
    for a in articles:
        cat=a.get("category","Misc")
        grouped.setdefault(cat,[]).append(a)

    for cat in order:
        items = grouped.get(cat,[])
        if not items: continue
        story.append(Paragraph(CATEGORY_LABELS.get(cat,cat), styles["Section"]))
        story.append(Spacer(1,6))
        for it in items:
            # card border using HR + background-like approach (simple)
            # Title
            story.append(Paragraph(f"<b>{it.get('section_heading','Untitled')}</b>", styles["Title"]))
            # small meta line
            meta = f"{it.get('upsc_relevance','')} • Source: {it.get('source','')}"
            story.append(Paragraph(meta, styles["SmallGray"]))
            story.append(Spacer(1,4))

            # image (right-aligned) placed before textual contents so it displays near top
            img_elem = None
            if it.get("image_bytes"):
                img_elem = make_image_element_simple(it.get("image_bytes"))
            if img_elem:
                story.append(img_elem)
                story.append(Spacer(1,4))

            # Context & About
            if it.get("context"):
                for p in split_into_paragraphs(it.get("context",""), chunk=800):
                    story.append(Paragraph(f"<b>Context:</b> {p}", styles["Body"]))
            if it.get("about"):
                for p in split_into_paragraphs(it.get("about",""), chunk=800):
                    story.append(Paragraph(f"<b>About:</b> {p}", styles["Body"]))

            # Facts & Data
            facts = it.get("facts_and_policies",[]) or []
            if facts:
                story.append(Paragraph("<b>Facts & Data:</b>", styles["Body"]))
                bullets = [ListItem(Paragraph(f, styles["Body"])) for f in facts]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            # Policy / Subsections
            subs = it.get("sub_sections",[]) or []
            for s in subs:
                head = s.get("heading","")
                pts = s.get("points",[]) or []
                if head:
                    story.append(Paragraph(f"<b>{head}:</b>", styles["Body"]))
                if pts:
                    bullets = [ListItem(Paragraph(p, styles["Body"])) for p in pts]
                    story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            # Detailed brief
            if it.get("detailed_brief"):
                story.append(Paragraph("<b>Detailed Brief:</b>", styles["Body"]))
                for p in split_into_paragraphs(it.get("detailed_brief",""), chunk=800):
                    story.append(Paragraph(p, styles["Body"]))

            # Impact
            impact = it.get("impact_or_analysis",[]) or []
            if impact:
                story.append(Paragraph("<b>Impact / Analysis:</b>", styles["Body"]))
                bullets = [ListItem(Paragraph(p, styles["Body"])) for p in impact]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            # end card with thin separator
            story.append(Spacer(1,8))
            hr = HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cfdff0"))
            story.append(hr)
            story.append(Spacer(1,8))

    # footer
    story.append(Paragraph("Note: Auto-generated summaries; verify facts from original sources if needed.", ParagraphStyle(name="note", fontSize=8, textColor=colors.grey)))

    try:
        doc.build(story)
        return out_path if (out_path:=out_path) else out_path
    except Exception as e:
        print("PDF build failed (final fallback):", e)
        return None

# ---------------- Email (unchanged) ----------------
def email_pdf_file(path):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError("Set SMTP_USER / SMTP_PASSWORD / EMAIL_TO env variables")
    msg = EmailMessage()
    msg["Subject"] = f"UPSC AI Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content("Attached: UPSC AI Current Affairs Brief (auto-generated).")
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(path))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------------- Main (assemble pipeline) ----------------
def process_and_run(test_url=None):
    date_str = datetime.date.today().isoformat()
    out_pdf = PDF_FILENAME_TEMPLATE.format(date=(date_str if not test_url else "TEST"))
    candidates=[]
    if test_url:
        candidates.append({"title":"Test article", "link": test_url})
    else:
        for feed in RSS_FEEDS:
            try:
                f = feedparser.parse(feed)
                for e in f.entries[:12]:
                    title = e.get("title",""); link = e.get("link","")
                    if title and link: candidates.append({"title":title,"link":link})
                    if len(candidates) >= MAX_CANDIDATES: break
            except Exception as ex:
                print("Feed error", feed, ex)
    print("Candidates:", len(candidates))

    processed=[]
    included=0; seen=set()
    for c in candidates:
        if included >= MAX_INCLUSIONS: break
        title=c["title"].strip(); link=c["link"]
        if link in seen: continue
        seen.add(link)
        print("Processing:", title)
        text,img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text, skip"); continue
        if is_boilerplate(title, text):
            print(" -> boilerplate skip"); continue
        # basic India relevance check
        if not (("india" in (title+" "+text).lower()) or any(d in link for d in [".gov.in","insightsonindia","drishtiias","pib.gov.in","prsindia"])):
            print(" -> not India-relevant (skipping)"); continue

        parsed, used_model = process_article(title, link, text, img)
        if str(parsed.get("include","yes")).lower() != "yes":
            print(" -> marked not relevant by model, skipping"); continue

        print(" -> included as", parsed.get("category"), "model_used=", used_model)
        processed.append(parsed)
        included += 1
        time.sleep(0.5)

    if not processed:
        print("No relevant items found. Exiting.")
        return

    pdf_path = build_pdf_simple(processed, out_pdf)
    if not pdf_path:
        print("PDF creation failed.")
        return
    print("PDF created:", pdf_path)

    if test_url:
        print("Test mode: not emailing. Inspect", pdf_path)
    else:
        try:
            email_pdf_file(pdf_path)
        except Exception as e:
            print("Email failed:", e)

# ---------------- CLI ----------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Generate TEST PDF from a single URL", default=None)
    args = parser.parse_args()
    process_and_run(test_url=args.test_url)