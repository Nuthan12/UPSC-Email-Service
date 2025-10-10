#!/usr/bin/env python3
"""
generate_and_send.py

Full single-file generator for UPSC daily briefs:
- Avoids repeated sentences across Context / About / Detailed Brief
- Deduplicates facts/policy bullets
- Filters out Q/A / Mains practice content and blacklisted domains
- Uses model (Groq/OpenAI) when available, else offline summarizer with optional web enrichment
- Generates PDF and optionally sends email

Usage:
  python generate_and_send.py --test-url "https://example.com/article"

Environment variables:
  OPENAI_API_KEY, OPENAI_MODEL (optional)
  GROQ_API_KEY, GROQ_MODEL (optional)
  SERPAPI_KEY or BING_API_KEY (optional, for web enrichment)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO (for emailing)
"""

import os, re, io, sys, time, json, ssl, argparse, smtplib, datetime, requests, feedparser
from email.message import EmailMessage

# PDF / images
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, ListFlowable, ListItem, HRFlowable
from reportlab.lib import colors
from reportlab.lib.units import mm
from PIL import Image as PILImage, ImageDraw, ImageFont

# -------- CONFIG --------
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
    "https://www.drishtiias.com/feed",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://prsindia.org/theprsblog/feed",
    "https://www.thehindu.com/news/national/feeder/default.rss",
    # insightsonindia excluded by default (reference-only)
]

MAX_CANDIDATES = 40
MAX_INCLUSIONS = 12

BLACKLIST_DOMAINS = ["insightsonindia.com"]

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

# -------- Utilities --------
def domain_from_url(url):
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except:
        return ""

def clean_text(raw):
    if not raw: return ""
    s = raw.replace("\r", "\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading", r"FOLLOW US", r"Download PDF"]
    for p in junk:
        s = re.sub(p, " ", s, flags=re.I)
    # collapse repeated identical lines (common site duplication)
    lines = [ln.rstrip() for ln in s.splitlines() if ln.strip()]
    cleaned = []
    prev = None
    repeats = 0
    for ln in lines:
        if ln == prev:
            repeats += 1
            if repeats > 1:
                continue
        else:
            repeats = 0
        cleaned.append(ln)
        prev = ln
    s = "\n".join(cleaned)
    s = re.sub(r'\n{3,}', '\n\n', s)
    s = re.sub(r'[\u200b-\u200f]', '', s)
    return s.strip()

def safe_trim(text, max_chars=3800):
    if not text: return ""
    if len(text) <= max_chars: return text
    w = text[:max_chars]
    last = max(w.rfind('.'), w.rfind('?'), w.rfind('!'))
    if last > int(max_chars*0.6):
        return w[:last+1]
    ln = w.rfind('\n')
    if ln > int(max_chars*0.5):
        return w[:ln]
    return re.sub(r'\s+\S*?$', '', w)

def extract_json_substring(s):
    if not s: return None
    i = s.find('{')
    if i == -1: return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{': depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j+1])
                except:
                    return None
    return None

# -------- Deduplication --------
def split_sentences_unique(text):
    if not text: return []
    sents = [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]
    seen = set(); out=[]
    for s in sents:
        key = re.sub(r'\s+', ' ', s.lower())[:300]
        if key in seen: continue
        seen.add(key); out.append(s)
    return out

def dedupe_paragraphs_list(pars):
    if not pars: return []
    seen=set(); out=[]
    for p in pars:
        key = re.sub(r'\s+', ' ', p.strip().lower())[:400]
        if key in seen: continue
        seen.add(key); out.append(p.strip())
    return out

def dedupe_sentences_in_text(text):
    if not text: return ""
    sents = split_sentences_unique(text)
    return " ".join(sents)

# -------- Q/A and boilerplate detection --------
QUESTION_KEYWORDS = ["mains","answer writing","answer","question","key demand","instalinks","mains practice","model answer","answer must","practice question"]
QUESTION_MARKERS = [r'\bQ[0-9]\b', r'\bQ1\b', r'\bQ2\b', r'\bQ3\b', r'\bQ4\b']

def is_question_article(title, text):
    t=(title or "").lower(); b=(text or "").lower()
    for k in QUESTION_KEYWORDS:
        if k in t: return True
    if re.search(r'key demand of the question|answer must|model answer|marking scheme|how to answer', b): return True
    for pat in QUESTION_MARKERS:
        if re.search(pat, b): return True
    if re.search(r'\b(upsc mains|mains enrichment|mains answer|mains practice)\b', b): return True
    directives = re.findall(r'\b(discuss|explain|analyse|critically|comment on|what are)\b', b)
    if len(directives) >= 3 and len(b.split()) < 1000: return True
    return False

def is_boilerplate(title, text):
    c = (title + " " + (text or "")).lower()
    patterns = ["upsc current affairs","instalinks","insta links","covers important current affairs","gs paper","content for mains enrichment","subscribe","answer writing"]
    return sum(1 for p in patterns if p in c) >= 2

def is_india_relevant(title, text, url):
    c = (title + " " + (text or "")).lower()
    if "india" in c or "indian" in c: return True
    if any(d in url for d in [".gov.in","drishtiias","pib.gov.in","prsindia"]): return True
    allow = ["nobel","climate","un","summit","report","treaty","agreement","world bank","imf"]
    return any(a in c for a in allow)

# -------- Article extraction --------
def extract_article_text_and_image(url, timeout=14):
    headers={"User-Agent":"Mozilla/5.0"}
    # newspaper3k
    try:
        from newspaper import Article
        art = Article(url); art.download(); art.parse()
        raw = art.text or ""
        raw = clean_text(raw)
        img_url = getattr(art, "top_image", None)
        img_bytes = None
        if img_url:
            try:
                r = requests.get(img_url, timeout=6, headers=headers)
                if r.status_code==200 and 'image' in r.headers.get('Content-Type',''):
                    img_bytes = r.content
            except: img_bytes=None
        if raw and len(split_sentences_unique(raw)) > 3:
            return raw, img_bytes
    except Exception:
        pass
    # readability fallback
    try:
        r = requests.get(url, timeout=12, headers=headers)
        from readability import Document
        doc = Document(r.text)
        summary_html = doc.summary()
        txt = re.sub(r'<[^>]+>', ' ', summary_html)
        txt = clean_text(txt)
        img_bytes=None
        m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=6, headers=headers)
                if r2.status_code==200 and 'image' in r2.headers.get('Content-Type',''):
                    img_bytes = r2.content
            except: img_bytes=None
        if txt and len(split_sentences_unique(txt)) > 3:
            return txt, img_bytes
    except Exception:
        pass
    # raw html fallback
    try:
        r = requests.get(url, timeout=12, headers=headers)
        html = re.sub(r'(?is)<(script|style|noscript).*?>.*?(</\1>)', ' ', r.text)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        text = clean_text(text)
        return text, None
    except Exception:
        return "", None

# -------- Model calls --------
def call_groq(prompt, max_tokens=1200):
    if not GROQ_API_KEY: return None
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"}
        payload = {"model":GROQ_MODEL,"messages":[{"role":"user","content":prompt}],"temperature":0.0,"max_tokens":max_tokens}
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code==200:
            content = r.json()["choices"][0]["message"]["content"]
            return extract_json_substring(content)
    except Exception as e:
        print("Groq error:", e)
    return None

def call_openai(prompt, max_tokens=1200):
    if not OPENAI_API_KEY: return None
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"}
        payload = {"model":OPENAI_MODEL,"messages":[{"role":"user","content":prompt}],"temperature":0.0,"max_tokens":max_tokens}
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
  "category":"GS1/GS2/GS3/GS4/CME/FFP/Misc",
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
- context: 1-2 crisp sentences.
- about: 2-4 background sentences (non-overlapping with context).
- facts_and_policies: 3-6 factual bullets (dates, numbers, ministries, acts).
- policy_points: key policy mentions.
- detailed_brief: 140-220 words synthesis (no repetition).
- impact_or_analysis: 3-5 concise implications.

Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    parsed=None
    if GROQ_API_KEY:
        parsed = call_groq(prompt)
        if parsed: print("[model] Groq parsed")
    if parsed is None and OPENAI_API_KEY:
        parsed = call_openai(prompt)
        if parsed: print("[model] OpenAI parsed")
    return parsed

# -------- Offline summarizer & web enrichment --------
FACT_PATTERNS = [r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b\d{1,3}(?:,\d{3})+\b', r'\b(Ministry|ICMR|NITI Aayog|WHO|World Bank|UN|IMF|RBI|Supreme Court)\b']
def split_sentences(text): return [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]

def make_context_offline(sents):
    return " ".join(sents[:2]) if sents else ""

def make_about_offline(sents):
    return " ".join(sents[2:6]) if len(sents)>2 else " ".join(sents[:3]) if sents else ""

def extract_facts_offline(text, max_b=6):
    s = split_sentences(text); scored=[]
    for sent in s:
        sc=0
        for pat in FACT_PATTERNS:
            if re.search(pat, sent, flags=re.I): sc+=1
        if sc>0: scored.append((sc, sent))
    scored.sort(key=lambda x:x[0], reverse=True)
    bullets=[]; used=set()
    for _,sent in scored:
        b = re.sub(r'\s+', ' ', sent).strip()
        if len(b)>220: b=b[:220].rsplit(' ',1)[0]+'...'
        if b not in used:
            bullets.append(b); used.add(b)
        if len(bullets)>=max_b: break
    if not bullets:
        for s in split_sentences(text)[:max_b]:
            bullets.append(s if len(s)<220 else s[:220]+"...")
    return bullets

def extract_policy_points_offline(text, max_n=4):
    s = split_sentences(text); pts=[]
    for sent in s:
        if re.search(r'\b(Ministry|Department|Scheme|Policy|Act|Bill|NITI Aayog|Prime Minister|Cabinet)\b', sent, flags=re.I):
            p = re.sub(r'\s+',' ', sent).strip()
            if len(p)>220: p=p[:220].rsplit(' ',1)[0]+'...'
            if p not in pts: pts.append(p)
        if len(pts)>=max_n: break
    return pts

def serpapi_search(q,num=3):
    if not SERPAPI_KEY: return []
    try:
        r = requests.get("https://serpapi.com/search.json", params={"q":q,"api_key":SERPAPI_KEY,"num":num}, timeout=12)
        js = r.json(); res=[]
        for it in js.get("organic_results", [])[:num]:
            res.append({"title": it.get("title"), "link": it.get("link"), "snippet": it.get("snippet")})
        return res
    except Exception as e:
        print("SerpAPI error:", e)
    return []

def bing_search(q,num=3):
    if not BING_API_KEY: return []
    try:
        r = requests.get("https://api.bing.microsoft.com/v7.0/search", headers={"Ocp-Apim-Subscription-Key":BING_API_KEY}, params={"q":q,"count":num}, timeout=12)
        js = r.json(); res=[]
        for it in js.get("webPages", {}).get("value", [])[:num]:
            res.append({"title": it.get("name"), "link": it.get("url"), "snippet": it.get("snippet")})
        return res
    except Exception as e:
        print("Bing error:", e)
    return []

def fetch_text_for_url(url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200: return ""
        from readability import Document
        doc = Document(r.text); txt = re.sub(r'<[^>]+>',' ', doc.summary())
        return clean_text(txt)
    except Exception:
        return ""

def web_enrich(title, text):
    q = title
    if re.search(r'\byojana\b|\bscheme\b|\bpradhan\b|\bmission\b', title, flags=re.I):
        q = title + " scheme details government website"
    results = serpapi_search(q, num=3) if SERPAPI_KEY else []
    if not results and BING_API_KEY:
        results = bing_search(q, num=3)
    web_facts=[]; web_policies=[]; sources=[]
    for it in results:
        url = it.get("link"); sources.append(url)
        txt = fetch_text_for_url(url)
        if not txt: continue
        wf = extract_facts_offline(txt, max_b=6); wp = extract_policy_points_offline(txt, max_n=6)
        for f in wf:
            if f not in web_facts: web_facts.append(f)
        for p in wp:
            if p not in web_policies: web_policies.append(p)
    return {"web_facts": web_facts, "web_policies": web_policies, "sources": sources}

# -------- process_article (complete with non-overlap and dedupe) --------
def process_article(title, url, text, img_bytes):
    # model summary attempt
    parsed = summarize_via_model(title, url, text) if (GROQ_API_KEY or OPENAI_API_KEY) else None
    used_model = bool(parsed)
    if not parsed: parsed = {}

    # cleaning and unique sentences
    core_text = clean_text(text)
    unique_sents = split_sentences_unique(core_text)

    # Context (first 1-2 unique sentences)
    context = parsed.get("context") or make_context_offline(unique_sents)
    # About (next 2-4 unique sentences, explicitly skipping context sentences)
    about = parsed.get("about") or make_about_offline(unique_sents)

    # Ensure non-overlap: remove sentences present in context from about
    ctx_keys = set(re.sub(r'\s+',' ', s.lower())[:300] for s in split_sentences_unique(context))
    about_sents = split_sentences_unique(about)
    about_filtered = [s for s in about_sents if re.sub(r'\s+',' ', s.lower())[:300] not in ctx_keys]
    about = " ".join(about_filtered) if about_filtered else about

    parsed["context"] = dedupe_sentences_in_text(context)
    parsed["about"] = dedupe_sentences_in_text(about)

    # Facts: model bullets preferred, else offline
    facts = parsed.get("facts_and_policies") or []
    if isinstance(facts, str):
        facts = [f.strip() for f in re.split(r'\n+|;|\u2022', facts) if f.strip()]
    if not facts or len([f for f in facts if len(f.strip())>8]) < 2:
        facts_off = extract_facts_offline(" ".join(unique_sents[:30]), max_b=6)
        facts = (facts + facts_off) if facts else facts_off
    facts = dedupe_paragraphs_list(facts)[:8]
    parsed["facts_and_policies"] = facts

    # Policy points
    policies = parsed.get("policy_points") or []
    if isinstance(policies, str):
        policies = [p.strip() for p in re.split(r'\n+|;|\u2022', policies) if p.strip()]
    if not policies:
        policies = extract_policy_points_offline(" ".join(unique_sents[:40]), max_n=6)
    policies = dedupe_paragraphs_list(policies)[:8]
    parsed["policy_points"] = policies

    # sub_sections normalization
    subs = parsed.get("sub_sections") or []
    normalized_subs=[]
    for s in subs:
        if isinstance(s, dict):
            heading = s.get("heading",""); pts = s.get("points",[]) or []
            if isinstance(pts, str): pts = [p.strip() for p in re.split(r'\n+|;|\u2022', pts) if p.strip()]
        else:
            heading=""; pts = s if isinstance(s, list) else []
        pts = dedupe_paragraphs_list(pts)
        normalized_subs.append({"heading": heading, "points": pts})
    if policies:
        normalized_subs.append({"heading":"Key Provisions / Policy Mentions","points": policies})
    parsed["sub_sections"] = normalized_subs

    # Detailed brief: compose from unique sentences excluding context/about
    used_keys = set(re.sub(r'\s+',' ', s.lower())[:300] for s in split_sentences_unique(parsed["context"] + " " + parsed["about"]))
    remaining = [s for s in unique_sents if re.sub(r'\s+',' ', s.lower())[:300] not in used_keys]
    # prefer model's detailed_brief if valid
    dbrief = parsed.get("detailed_brief") or ""
    if isinstance(dbrief, list): dbrief = " ".join(dbrief)
    dbrief = dbrief.strip()
    if not dbrief or len(split_sentences_unique(dbrief)) < 3:
        # create synthetic brief: about + top 3 remaining sentences + top facts + a short implication line
        parts=[]
        if parsed["about"]: parts.append(parsed["about"])
        parts.extend(remaining[:3])
        parts.extend(parsed.get("facts_and_policies",[])[:2])
        if parsed.get("policy_points"): parts.append(parsed["policy_points"][0])
        parts.append("Implications: See impact/analysis below.")
        dbrief = " ".join([p for p in parts if p])
    dbrief = dedupe_sentences_in_text(dbrief)
    # length constraints: 140-220 words desirable; if shorter, append focused implication sentences
    if len(dbrief.split()) < 140:
        add_imp=[]
        for f in parsed.get("facts_and_policies",[])[:3]:
            short = f.split('.')[0]
            add_imp.append(f"Implication: {short} — may affect policy and implementation.")
        dbrief = dbrief + " " + " ".join(add_imp)
    words = dbrief.split()
    if len(words) > 220:
        dbrief = " ".join(words[:220])
    parsed["detailed_brief"] = dbrief

    # Impact / Analysis
    impact = parsed.get("impact_or_analysis") or []
    if isinstance(impact, str):
        impact = [p.strip() for p in re.split(r'\n+|;|\u2022', impact) if p.strip()]
    if not impact or len(impact) < 2:
        impact = []
        if policies:
            impact.append(f"Policy implication: {policies[0]}")
        if facts:
            impact.append(f"Operational impact: {facts[0]}")
        impact.append("Implementation challenge: coordination between stakeholders required.")
    parsed["impact_or_analysis"] = dedupe_paragraphs_list(impact)

    # category heuristics
    cat = parsed.get("category") or ""
    if cat:
        m = re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
        if m: parsed["category"] = f"GS{m.group(1)}"
    else:
        ctext = (title + " " + " ".join(unique_sents[:6])).lower()
        if any(k in ctext for k in ["constitution","parliament","supreme court","policy","minister","government"]): parsed["category"] = "GS2"
        elif any(k in ctext for k in ["economy","gdp","rbi","inflation","industry","agriculture","science","environment","climate","nobel"]): parsed["category"] = "GS3"
        elif any(k in ctext for k in ["ethic","ethics","corruption","integrity"]): parsed["category"] = "GS4"
        else: parsed["category"] = "Misc"

    # optional web enrichment if insufficient facts/policies
    need_web = (len(parsed.get("facts_and_policies",[])) < 3 or len(parsed.get("policy_points",[])) < 1)
    if need_web and (SERPAPI_KEY or BING_API_KEY):
        try:
            enr = web_enrich(title, core_text)
            wf = enr.get("web_facts", [])[:6]; wp = enr.get("web_policies", [])[:6]
            merged_facts = parsed.get("facts_and_policies", []) + [f for f in wf if f not in parsed.get("facts_and_policies", [])]
            merged_pols = parsed.get("policy_points", []) + [p for p in wp if p not in parsed.get("policy_points", [])]
            parsed["facts_and_policies"] = dedupe_paragraphs_list(merged_facts)[:8]
            parsed["policy_points"] = dedupe_paragraphs_list(merged_pols)[:8]
            if parsed.get("policy_points"):
                parsed["sub_sections"].append({"heading":"Key Provisions / Policy Mentions (web-enriched)","points": parsed["policy_points"]})
            parsed["web_sources"] = enr.get("sources", [])
            print(" -> web enrichment used")
        except Exception as e:
            print("web enrichment failed:", e)

    parsed["image_bytes"] = img_bytes
    parsed.setdefault("upsc_relevance", CATEGORY_LABELS.get(parsed.get("category","Misc"), parsed.get("category","Misc")))
    parsed.setdefault("source", url)
    parsed.setdefault("include", "yes")
    return parsed, used_model

# -------- PDF builder --------
def ensure_style(styles, name, **kwargs):
    if name in styles.byName: return styles.byName[name]
    ps = ParagraphStyle(name=name, **kwargs)
    try:
        styles.add(ps)
    except Exception:
        styles.byName[name] = ps
    return ps

def generate_logo_bytes(text="DailyCAThroughAI", size=(420,80), bgcolor=(31,78,121), fg=(255,255,255)):
    try:
        img = PILImage.new("RGB", size, bgcolor)
        draw = ImageDraw.Draw(img)
        try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        except: font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0,0), text, font=font); w=bbox[2]-bbox[0]; h=bbox[3]-bbox[1]
        except:
            w,h = draw.textsize(text, font=font)
        draw.text(((size[0]-w)/2,(size[1]-h)/2), text, font=font, fill=fg)
        bio=io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
        return bio.read()
    except Exception as e:
        print("logo error", e); return None

def make_image_element_simple(im_bytes, max_w=180, max_h=120):
    if not im_bytes: return None
    try:
        pil = PILImage.open(io.BytesIO(im_bytes)); pil.load()
        w,h = pil.size
        ratio = min(max_w/float(w), max_h/float(h), 1.0)
        new_w, new_h = int(w*ratio), int(h*ratio)
        pil = pil.resize((new_w,new_h), PILImage.LANCZOS)
        bb = io.BytesIO(); pil.save(bb, format='PNG'); bb.seek(0)
        img = RLImage(bb, width=new_w, height=new_h); img.hAlign='RIGHT'
        return img
    except Exception as e:
        print("image skipped", e); return None

def split_into_paragraphs(text, chunk=800):
    if not text: return []
    text=text.strip()
    if len(text) <= chunk: return [text]
    parts=[]
    start=0; L=len(text)
    while start < L:
        end = start + chunk
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

    story.append(Paragraph("Note: Auto-generated summaries; verify facts from official sources when needed.", ParagraphStyle(name="note", fontSize=8, textColor=colors.grey)))
    try:
        doc.build(story); return out_path
    except Exception as e:
        print("PDF build failed:", e); return None

# -------- Email --------
def email_pdf_file(path):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        print("SMTP not configured; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = f"UPSC AI Brief — {datetime.date.today().strftime('%d %b %Y')}"
    msg["From"] = SMTP_USER; msg["To"] = EMAIL_TO
    msg.set_content("Attached: UPSC AI Current Affairs Brief (auto-generated).")
    with open(path, "rb") as f: msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(path))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASSWORD); s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# -------- Main pipeline --------
def main(test_url=None):
    date_str = datetime.date.today().isoformat()
    output_pdf = PDF_FILENAME_TEMPLATE.format(date=(date_str if not test_url else "TEST"))
    candidates=[]

    if test_url:
        candidates.append({"title":"TEST", "link": test_url})
    else:
        for feed in RSS_FEEDS:
            try:
                f = feedparser.parse(feed)
                for e in f.entries[:12]:
                    title=e.get("title",""); link=e.get("link")
                    if title and link: candidates.append({"title":title,"link":link})
                    if len(candidates) >= MAX_CANDIDATES: break
            except Exception as ex:
                print("Feed error", feed, ex)

    print("Candidates:", len(candidates))
    processed=[]; included=0; seen=set()
    for c in candidates:
        if included >= MAX_INCLUSIONS: break
        title=c["title"].strip(); link=c["link"]
        if not link or link in seen: continue
        seen.add(link)
        dom = domain_from_url(link)
        if any(b in dom for b in BLACKLIST_DOMAINS):
            print("Skipping (blacklisted domain):", dom, title); continue
        print("Processing:", title)
        text,img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text; skip"); continue
        if is_boilerplate(title, text):
            print(" -> skipped boilerplate"); continue
        if is_question_article(title, text):
            print(" -> skipped Q/A or Mains practice"); continue
        if not is_india_relevant(title, text, link):
            print(" -> not India-relevant; skip"); continue

        parsed, used_model = process_article(title, link, text, img)
        if str(parsed.get("include","yes")).lower() != "yes":
            print(" -> model indicated not relevant; skipping"); continue

        processed.append(parsed); included += 1
        print(" -> included:", parsed.get("category"), "model_used=", used_model)
        time.sleep(0.35)

    if not processed:
        print("No relevant items found. Exiting."); return

    pdf_path = build_pdf_simple(processed, output_pdf)
    if not pdf_path:
        print("PDF generation failed."); return
    print("PDF created:", pdf_path)

    if test_url:
        print("Test mode — not emailing. Inspect", pdf_path)
    else:
        email_pdf_file(pdf_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Run on a single URL for testing", default=None)
    args = parser.parse_args()
    main(test_url=args.test_url)