#!/usr/bin/env python3
"""
generate_and_send.py — final complete script (fixed: style name collisions)

This is the same full script you had before, but it avoids adding duplicate
ReportLab stylesheet names (KeyError). It creates styles only if missing.
"""

import os
import re
import io
import sys
import time
import json
import ssl
import argparse
import smtplib
import datetime
import requests
import feedparser
from email.message import EmailMessage
from pprint import pprint

# PDF and image libs
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

# Feeds chosen for UPSC-relevant content
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
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    last_p = max(window.rfind('.'), window.rfind('?'), window.rfind('!'))
    if last_p > int(max_chars * 0.6):
        return window[:last_p+1]
    last_n = window.rfind('\n')
    if last_n > int(max_chars * 0.5):
        return window[:last_n]
    return re.sub(r'\s+\S*?$', '', window)

def clean_text(raw):
    if not raw:
        return ""
    t = raw.replace("\r", "\n")
    junk = [r"SEE ALL NEWSLETTERS", r"ADVERTISEMENT", r"Subscribe", r"Read more", r"Continue reading"]
    for p in junk:
        t = re.sub(p, " ", t, flags=re.I)
    lines = [ln.strip() for ln in t.splitlines() if len(ln.strip()) > 30 and not re.match(r'^[A-Z\s]{15,}$', ln.strip())]
    out = "\n\n".join(lines)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()

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

def is_boilerplate(title, text):
    c = (title + " " + (text or "")).lower()
    patterns = ["upsc current affairs", "instalinks", "covers important current affairs", "gs paper", "content for mains enrichment"]
    return sum(1 for p in patterns if p in c) >= 2

def is_india_relevant(title, text, url):
    c = (title + " " + (text or "")).lower()
    if "india" in c or "indian" in c:
        return True
    if any(d in url for d in [".gov.in", "insightsonindia", "drishtiias", "pib.gov.in", "prsindia"]):
        return True
    allow = ["nobel", "climate", "un", "summit", "report", "treaty", "agreement", "world bank", "imf"]
    return any(a in c for a in allow)

# ---------------- Article extraction ----------------
def extract_article_text_and_image(url, timeout=12):
    headers = {"User-Agent": "Mozilla/5.0"}
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
                if r.status_code == 200 and 'image' in r.headers.get('Content-Type', ''):
                    img_bytes = r.content
            except:
                img_bytes = None
        if text and len(text.split()) > 50:
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
        img_bytes = None
        m = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', r.text, flags=re.I)
        if m:
            try:
                r2 = requests.get(m.group(1), timeout=timeout, headers=headers)
                if r2.status_code == 200 and 'image' in r2.headers.get('Content-Type', ''):
                    img_bytes = r2.content
            except:
                img_bytes = None
        if text and len(text.split()) > 50:
            return text, img_bytes
    except Exception:
        pass
    # html strip fallback
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', ' ', r.text)
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = ' '.join(stripped.split())
        text = clean_text(stripped)
        if text and len(text.split()) > 50:
            return text, None
    except Exception:
        pass
    return "", None

# ---------------- Model calls ----------------
def call_groq(prompt, max_tokens=1200):
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            parsed = extract_json_substring(content)
            return parsed
    except Exception as e:
        print("Groq error:", e)
    return None

def call_openai(prompt, max_tokens=1200):
    if not OPENAI_API_KEY:
        return None
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": max_tokens}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            parsed = extract_json_substring(content)
            return parsed
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
- Provide 3-6 factual bullets (schemes, ministries, report names, dates, numbers) in 'facts_and_policies'.
- Provide policy/scheme/act mentions in 'policy_points' or under 'sub_sections'.
- Provide 'detailed_brief' 120-220 words synthesizing causes, facts and implications for UPSC.
- If text is truncated, set "context" to "SOURCE_ONLY".
- Do NOT invent facts.

Title: {title}
URL: {url}
Article Text: {trimmed}
"""
    parsed = None
    if GROQ_API_KEY:
        parsed = call_groq(prompt)
        if parsed:
            print("[model] Groq parsed JSON (truncated):", str(parsed)[:400])
    if parsed is None and OPENAI_API_KEY:
        parsed = call_openai(prompt)
        if parsed:
            print("[model] OpenAI parsed JSON (truncated):", str(parsed)[:400])
    return parsed

# -------------- Offline deterministic summarizer --------------
FACT_PATTERNS = [
    r'\b\d{4}\b', r'\b\d+%|\d+\.\d+%', r'\b\d{1,3}(?:,\d{3})+\b',
    r'\b(ICMR|ISRO|NITI Aayog|WHO|World Bank|UN|KAUST|IMF)\b',
    r'\b(Ministry of|Department of|Scheme|Policy|Act|Bill|Programme|Program)\b',
    r'\b(report|index|survey)\b'
]

def extract_sentences(text):
    sents = [s.strip() for s in re.split(r'(?<=[\.\?\!])\s+', text) if s.strip()]
    return sents

def make_context(text):
    sents = extract_sentences(text)
    if not sents:
        return ""
    return " ".join(sents[:2])[:600]

def make_about(text):
    sents = extract_sentences(text)
    if len(sents) >= 4:
        about = " ".join(sents[1:4])
    elif len(sents) >= 2:
        about = " ".join(sents[0:2])
    else:
        about = text[:400]
    return about

def extract_facts(text, max_bullets=6):
    sents = extract_sentences(text)
    scored = []
    for s in sents:
        score = 0
        for pat in FACT_PATTERNS:
            if re.search(pat, s, flags=re.I):
                score += 1
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    bullets = []
    seen = set()
    for _, s in scored:
        b = re.sub(r'\s+', ' ', s).strip()
        if len(b) > 200:
            b = b[:200].rsplit(' ', 1)[0] + '...'
        if b not in seen:
            bullets.append(b)
            seen.add(b)
        if len(bullets) >= max_bullets:
            break
    if not bullets:
        nums = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?', text)
        for n in nums[:max_bullets]:
            bullets.append(f"Figure: {n}")
    return bullets

def extract_policy_points(text, max_items=4):
    sents = extract_sentences(text)
    pts = []
    for s in sents:
        if re.search(r'\b(Ministry|Department|Policy|Scheme|Act|Bill|NITI Aayog|Prime Minister|PM)\b', s, flags=re.I):
            p = re.sub(r'\s+', ' ', s).strip()
            if len(p) > 200:
                p = p[:200].rsplit(' ', 1)[0] + '...'
            if p not in pts:
                pts.append(p)
        if len(pts) >= max_items:
            break
    return pts

def make_detailed_brief(title, about, facts, policy_points):
    parts = []
    if about:
        parts.append(about)
    parts.extend(facts[:3])
    if policy_points:
        parts.append(policy_points[0])
    para = " ".join(parts)
    if len(para) < 120:
        para = para + " " + (" ".join(facts[3:5])) if len(facts) > 3 else para
    return para[:1200]

def make_impact(text):
    sents = extract_sentences(text)
    impacts = []
    for s in sents:
        if len(s) > 60 and any(w in s.lower() for w in ["impact", "challenge", "concern", "affect", "threat", "benefit", "important", "key"]):
            impacts.append(s if len(s) < 200 else s[:200] + "...")
        if len(impacts) >= 4:
            break
    if not impacts:
        impacts = [
            "Significance for policy and governance.",
            "Potential implications for stakeholders and implementation."
        ]
    return impacts

# ---------------- Web enrichment ----------------
def serpapi_search(query, num=3):
    if not SERPAPI_KEY:
        return []
    url = "https://serpapi.com/search.json"
    params = {"q": query, "api_key": SERPAPI_KEY, "num": num}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            js = r.json()
            results = []
            for item in js.get("organic_results", [])[:num]:
                results.append({"title": item.get("title"), "link": item.get("link"), "snippet": item.get("snippet")})
            return results
    except Exception as e:
        print("SerpAPI error:", e)
    return []

def bing_search(query, num=3):
    if not BING_API_KEY:
        return []
    url = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
    params = {"q": query, "count": num, "textDecorations": False, "textFormat": "Raw"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            js = r.json()
            results = []
            for item in js.get("webPages", {}).get("value", [])[:num]:
                results.append({"title": item.get("name"), "link": item.get("url"), "snippet": item.get("snippet")})
            return results
    except Exception as e:
        print("Bing search error:", e)
    return []

def fetch_and_extract_plain_text(url, timeout=12):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return ""
        from readability import Document
        doc = Document(r.text)
        summary = doc.summary()
        text = re.sub(r'<[^>]+>', ' ', summary)
        text = clean_text(text)
        if len(text) < 100:
            stripped = re.sub(r'<[^>]+>', ' ', r.text)
            text = clean_text(stripped)
        return text
    except Exception:
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            stripped = re.sub(r'<[^>]+>', ' ', r.text)
            return clean_text(stripped)
        except:
            return ""

def web_enrich(title, text, max_results=3):
    query_base = title
    if re.search(r'\byojana\b|\bscheme\b|\bpradhan\b|\bmission\b|\bprogram\b|\bprogramme\b', title, flags=re.I):
        query_base = title + " scheme details government official website"
    results = serpapi_search(query_base, num=max_results) if SERPAPI_KEY else []
    if not results:
        results = bing_search(query_base, num=max_results) if BING_API_KEY else []
    web_facts = []
    web_policies = []
    sources = []
    for r in results:
        link = r.get("link")
        if not link:
            continue
        sources.append(link)
        page_text = fetch_and_extract_plain_text(link)
        if not page_text:
            continue
        facts = extract_facts(page_text, max_bullets=6)
        policies = extract_policy_points(page_text, max_items=6)
        for f in facts:
            if f not in web_facts:
                web_facts.append(f)
        for p in policies:
            if p not in web_policies:
                web_policies.append(p)
    return {"web_facts": web_facts, "web_policies": web_policies, "sources": sources}

# ---------------- Process article (model + offline + web) ----------------
def process_article(title, url, text, img_bytes):
    parsed = summarize_via_model(title, url, text)
    used_model = parsed is not None
    if not parsed:
        parsed = {}

    parsed["include"] = str(parsed.get("include", "yes"))
    parsed["section_heading"] = parsed.get("section_heading") or title
    if not parsed.get("context"):
        parsed["context"] = make_context(text)
    if not parsed.get("about"):
        parsed["about"] = make_about(text)

    facts = parsed.get("facts_and_policies") or []
    if not facts or len([f for f in facts if len(f.strip()) > 8]) < 2:
        offline_facts = extract_facts(text, max_bullets=6)
        parsed["facts_and_policies"] = (facts + offline_facts)[:8]
    else:
        parsed["facts_and_policies"] = facts[:8]

    policy_points = parsed.get("policy_points") or []
    if not policy_points:
        parsed["policy_points"] = extract_policy_points(text, max_items=4)

    subs = parsed.get("sub_sections") or []
    if parsed.get("policy_points"):
        subs.append({"heading": "Key Provisions / Policy Mentions", "points": parsed["policy_points"]})
    parsed["sub_sections"] = subs

    if not parsed.get("detailed_brief") or len(parsed.get("detailed_brief", "").strip()) < 120:
        parsed["detailed_brief"] = make_detailed_brief(title, parsed.get("about", ""), parsed.get("facts_and_policies", []), parsed.get("policy_points", []))

    if not parsed.get("impact_or_analysis"):
        parsed["impact_or_analysis"] = make_impact(text)

    cat = parsed.get("category") or ""
    if cat:
        m = re.search(r'gs\s*([1-4])', str(cat), flags=re.I)
        if m:
            parsed["category"] = f"GS{m.group(1)}"
    else:
        ctext = (title + " " + text).lower()
        if any(k in ctext for k in ["constitution", "parliament", "supreme court", "policy", "minister", "government"]):
            parsed["category"] = "GS2"
        elif any(k in ctext for k in ["economy", "gdp", "rbi", "inflation", "industry", "agriculture", "isro", "science", "environment", "climate", "nobel"]):
            parsed["category"] = "GS3"
        elif any(k in ctext for k in ["ethic", "ethics", "corruption", "integrity"]):
            parsed["category"] = "GS4"
        elif any(k in ctext for k in ["study", "report", "survey", "analysis", "index"]):
            parsed["category"] = "CME"
        else:
            parsed["category"] = "Misc"

    need_web = False
    if len(parsed.get("facts_and_policies", [])) < 3 or len(parsed.get("policy_points", [])) < 1:
        need_web = True
    if re.search(r'\byojana\b|\bscheme\b|\bpradhan\b|\bmission\b|\bprogram\b|\bprogramme\b', title, flags=re.I):
        need_web = True

    if need_web and (SERPAPI_KEY or BING_API_KEY):
        try:
            enrich = web_enrich(title, text, max_results=4)
            wf = enrich.get("web_facts", [])[:6]
            wp = enrich.get("web_policies", [])[:6]
            merged_facts = parsed.get("facts_and_policies", []) + [f for f in wf if f not in parsed.get("facts_and_policies", [])]
            merged_policies = parsed.get("policy_points", []) + [p for p in wp if p not in parsed.get("policy_points", [])]
            parsed["facts_and_policies"] = merged_facts[:8]
            parsed["policy_points"] = merged_policies[:8]
            if parsed.get("policy_points"):
                subs = parsed.get("sub_sections", []) or []
                subs.append({"heading": "Key Provisions / Policy Mentions (web-enriched)", "points": parsed["policy_points"]})
                parsed["sub_sections"] = subs
            parsed["web_sources"] = enrich.get("sources", [])
            print(" -> web enrichment used; sources:", parsed.get("web_sources", [])[:3])
        except Exception as e:
            print("Web enrichment failed:", e)

    parsed["image_bytes"] = img_bytes
    parsed.setdefault("upsc_relevance", CATEGORY_LABELS.get(parsed["category"], parsed["category"]))
    parsed.setdefault("source", url)
    parsed.setdefault("include", "yes")
    parsed.setdefault("sub_sections", parsed.get("sub_sections", []))
    return parsed, used_model

# ---------------- PDF helpers & builder (simple, robust) ----------------
def ensure_style(stylesheet, name, **kwargs):
    """Add a ParagraphStyle to stylesheet if it does not already exist."""
    if name in stylesheet.byName:
        return stylesheet.byName[name]
    ps = ParagraphStyle(name=name, **kwargs)
    try:
        stylesheet.add(ps)
    except Exception:
        # fallback: if add fails for any odd reason, overwrite byName directly
        stylesheet.byName[name] = ps
    return ps

def generate_logo_bytes(text="DailyCAThroughAI", size=(420, 80), bgcolor=(31, 78, 121), fg=(255, 255, 255)):
    try:
        img = PILImage.new("RGB", size, bgcolor)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        except:
            font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]; h = bbox[3] - bbox[1]
        except:
            w, h = draw.textsize(text, font=font)
        x = (size[0] - w) / 2; y = (size[1] - h) / 2
        draw.text((x, y), text, font=font, fill=fg)
        bio = io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
        return bio.read()
    except Exception as e:
        print("logo error", e); return None

def make_image_element_simple(im_bytes, max_w=180, max_h=120):
    if not im_bytes:
        return None
    try:
        pil = PILImage.open(io.BytesIO(im_bytes)); pil.load()
        w, h = pil.size
        if w <= 0 or h <= 0 or w > 6000 or h > 6000:
            return None
        ratio = min(max_w / float(w), max_h / float(h), 1.0)
        new_w, new_h = int(w * ratio), int(h * ratio)
        pil = pil.resize((new_w, new_h), PILImage.LANCZOS)
        bb = io.BytesIO(); pil.save(bb, format="PNG"); bb.seek(0)
        img = RLImage(bb, width=new_w, height=new_h)
        img.hAlign = 'RIGHT'
        return img
    except Exception as e:
        print("make_image_element_simple skipped image:", e)
        return None

def split_into_paragraphs(text, chunk=800):
    if not text:
        return []
    text = text.strip()
    if len(text) <= chunk:
        return [text]
    parts = []
    start = 0
    L = len(text)
    while start < L:
        end = start + chunk
        if end < L:
            next_break = text.rfind('.', start, end)
            if next_break <= start:
                next_break = text.rfind(' ', start, end)
            if next_break <= start:
                next_break = end
            end = next_break
        parts.append(text[start:end].strip())
        start = end if end > start else start + chunk
    return parts

def build_pdf_simple(articles, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    # use ensure_style to avoid KeyError
    ensure_style(styles, "UPSC_Title", fontSize=13, leading=15)
    ensure_style(styles, "UPSC_Body", fontSize=10, leading=13)
    ensure_style(styles, "UPSC_Section", fontSize=12, leading=14, textColor=colors.HexColor("#1f4e79"))
    ensure_style(styles, "UPSC_SmallGray", fontSize=8, leading=10, textColor=colors.grey)

    story = []
    today = datetime.datetime.now().strftime("%d %B %Y")
    logo = generate_logo_bytes()
    if logo:
        img = RLImage(io.BytesIO(logo), width=120, height=32)
        img.hAlign = 'LEFT'
        story.append(img)
    story.append(Paragraph(f"<b>UPSC CURRENT AFFAIRS</b> — {today}", styles["UPSC_Title"]))
    story.append(Spacer(1, 8))

    order = ["GS1", "GS2", "GS3", "GS4", "CME", "FFP", "Mapping", "Misc"]
    grouped = {k: [] for k in order}
    for a in articles:
        cat = a.get("category", "Misc")
        grouped.setdefault(cat, []).append(a)

    for cat in order:
        items = grouped.get(cat, [])
        if not items:
            continue
        story.append(Paragraph(CATEGORY_LABELS.get(cat, cat), styles["UPSC_Section"]))
        story.append(Spacer(1, 6))
        for it in items:
            story.append(Paragraph(f"<b>{it.get('section_heading','Untitled')}</b>", styles["UPSC_Title"]))
            meta = f"{it.get('upsc_relevance','')} • Source: {it.get('source','')}"
            story.append(Paragraph(meta, styles["UPSC_SmallGray"]))
            story.append(Spacer(1, 4))

            img_elem = None
            if it.get("image_bytes"):
                img_elem = make_image_element_simple(it.get("image_bytes"))
            if img_elem:
                story.append(img_elem)
                story.append(Spacer(1, 4))

            if it.get("context"):
                for p in split_into_paragraphs(it.get("context", ""), chunk=800):
                    story.append(Paragraph(f"<b>Context:</b> {p}", styles["UPSC_Body"]))
            if it.get("about"):
                for p in split_into_paragraphs(it.get("about", ""), chunk=800):
                    story.append(Paragraph(f"<b>About:</b> {p}", styles["UPSC_Body"]))

            facts = it.get("facts_and_policies", []) or []
            if facts:
                story.append(Paragraph("<b>Facts & Data:</b>", styles["UPSC_Body"]))
                bullets = [ListItem(Paragraph(f, styles["UPSC_Body"])) for f in facts]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            subs = it.get("sub_sections", []) or []
            for s in subs:
                head = s.get("heading", ""); pts = s.get("points", []) or []
                if head:
                    story.append(Paragraph(f"<b>{head}:</b>", styles["UPSC_Body"]))
                if pts:
                    bullets = [ListItem(Paragraph(p, styles["UPSC_Body"])) for p in pts]
                    story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            if it.get("detailed_brief"):
                story.append(Paragraph("<b>Detailed Brief:</b>", styles["UPSC_Body"]))
                for p in split_into_paragraphs(it.get("detailed_brief", ""), chunk=800):
                    story.append(Paragraph(p, styles["UPSC_Body"]))

            impact = it.get("impact_or_analysis", []) or []
            if impact:
                story.append(Paragraph("<b>Impact / Analysis:</b>", styles["UPSC_Body"]))
                bullets = [ListItem(Paragraph(p, styles["UPSC_Body"])) for p in impact]
                story.append(ListFlowable(bullets, bulletType='bullet', leftIndent=12))

            story.append(Spacer(1, 8))
            hr = HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cfdff0"))
            story.append(hr)
            story.append(Spacer(1, 8))

    story.append(Paragraph("Note: Auto-generated summaries; verify facts from original sources if needed.", ParagraphStyle(name="note", fontSize=8, textColor=colors.grey)))
    try:
        doc.build(story)
        return out_path if (out_path := out_path) else out_path
    except Exception as e:
        print("PDF build failed:", e)
        return None

# ---------------- Email ----------------
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
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    print("Email sent to", EMAIL_TO)

# ---------------- Main ----------------
def main(test_url=None):
    date_str = datetime.date.today().isoformat()
    output_pdf = PDF_FILENAME_TEMPLATE.format(date=(date_str if not test_url else "TEST"))
    candidates = []

    if test_url:
        candidates.append({"title": "Test article", "link": test_url})
    else:
        for feed in RSS_FEEDS:
            try:
                parsed = feedparser.parse(feed)
                for entry in parsed.entries[:12]:
                    title = entry.get("title", "")
                    link = entry.get("link", "")
                    if title and link:
                        candidates.append({"title": title, "link": link})
                    if len(candidates) >= MAX_CANDIDATES:
                        break
            except Exception as ex:
                print("Feed error", feed, ex)
    print("Candidates:", len(candidates))

    processed = []
    included = 0
    seen = set()
    for c in candidates:
        if included >= MAX_INCLUSIONS:
            break
        title = c["title"].strip()
        link = c["link"]
        if link in seen:
            continue
        seen.add(link)
        print("Processing:", title)
        text, img = extract_article_text_and_image(link)
        if not text:
            print(" -> no text, skip"); continue
        if is_boilerplate(title, text):
            print(" -> boilerplate skipped"); continue
        if not is_india_relevant(title, text, link):
            print(" -> not India-relevant skip"); continue

        parsed, used_model = process_article(title, link, text, img)
        if str(parsed.get("include", "yes")).lower() != "yes":
            print(" -> model marked not relevant; skipping")
            continue

        print(f" -> included: category={parsed.get('category')} (model_used={used_model})")
        processed.append(parsed)
        included += 1
        time.sleep(0.6)

    if not processed:
        print("No relevant items found. Exiting.")
        return

    pdf_path = build_pdf_simple(processed, output_pdf)
    if not pdf_path:
        print("PDF generation failed.")
        return
    print("PDF created:", pdf_path)

    if test_url:
        print("Test mode — not emailing. Inspect", pdf_path)
    else:
        try:
            email_pdf_file(pdf_path)
        except Exception as e:
            print("Email failed:", e)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-url", help="Run script for a single test URL and produce TEST PDF", default=None)
    args = parser.parse_args()
    main(test_url=args.test_url)