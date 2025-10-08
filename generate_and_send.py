#!/usr/bin/env python3
"""generate_and_send.py
AI-powered UPSC daily brief generator:
 - Fetches curated RSS feeds
 - Extracts article text
 - Classifies into UPSC sections using OpenAI
 - Summarizes with OpenAI in the demo's style & depth
 - Builds a template PDF (InsightsonIndia-style)
 - Emails the PDF via SMTP

Required repo secrets (GitHub Actions):
 - OPENAI_API_KEY  (OpenAI API key)
 - SMTP_USER       (email address used to send)
 - SMTP_PASSWORD   (app password or SMTP password)
 - EMAIL_TO        (recipient email)

Optional environment variables (set in Actions or as repo secrets):
 - SMTP_HOST (default smtp.gmail.com)
 - SMTP_PORT (default 587)
 - PDF_PREFIX (default 'UPSC_current_affairs_ai_')

Notes:
 - This script is designed to be run inside GitHub Actions (Ubuntu runner).
 - Use a valid OpenAI API key for best-quality classification and summaries.
"""

import os, datetime, time, ssl, smtplib, textwrap
from email.message import EmailMessage
from urllib.parse import urlparse

# ---------- Configuration ----------
RSS_FEEDS = [
    # curated UPSC-relevant RSS feeds (add/remove as you wish)
    "https://www.thehindu.com/news/feeder/default.rss",
    "https://www.reuters.com/world/rss.xml",
    "https://pib.gov.in/AllRelFeeds.aspx?Format=RSS",
    "https://www.thehindu.com/opinion/lead/feeder/default.rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ndtv.com/rss/india.xml",
    "https://www.apnews.com/hub/ap-top-news/rss",
]

MAX_ARTICLES = 20  # cap per run to keep PDF focused
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # recommended model; change if needed

# ---------- Helpers ----------
def get_today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")

def safe_filename(s):
    return "".join(c for c in s if c.isalnum() or c in ' _-').rstrip()

# ---------- Fetch RSS entries ----------
def fetch_feed_entries():
    import feedparser
    entries = []
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for e in d.entries:
                published = None
                if hasattr(e, 'published_parsed') and e.published_parsed:
                    published = datetime.datetime(*e.published_parsed[:6])
                elif hasattr(e, 'updated_parsed') and e.updated_parsed:
                    published = datetime.datetime(*e.updated_parsed[:6])
                entries.append({
                    'title': e.title,
                    'link': e.link,
                    'published': published,
                    'source': urlparse(feed).hostname
                })
        except Exception as ex:
            print('Feed error for', feed, ex)
    entries = sorted(entries, key=lambda x: x['published'] or datetime.datetime(1970,1,1), reverse=True)
    return entries

# ---------- Extract main text ----------
def extract_article_text(url):
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        text = art.text
        if text and len(text.split())>40:
            return text
    except Exception as e:
        # fallback to requests
        pass
    try:
        import requests, re
        r = requests.get(url, timeout=10, headers={'User-Agent':'Mozilla/5.0'})
        html = r.text
        html = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', '', html)
        text = re.sub(r'(?s)<.*?>', ' ', html)
        text = ' '.join(text.split())
        if len(text)>200:
            return text
    except Exception as e:
        print('extract fallback failed', e)
    return ''

# ---------- OpenAI helpers ----------
def openai_classify_and_summarize(openai_key, title, text):
    # Use OpenAI Chat Completions API to classify + summarise in UPSC style
    import openai
    openai.api_key = openai_key
    prompt = f"""You are an assistant that converts news articles into UPSC-style current affairs writeups.
Given an article title and the article text, perform these tasks and return a JSON object ONLY with the following keys:
- category: one of [GS1, GS2, GS3, GS4, FFP, CME, Mapping, Misc]
- section_heading: short heading suitable for the PDF
- context: 1-2 sentence context line
- key_points: 2-4 short bullet-style sentences (comma-separated in JSON)
- significance: 1 short sentence describing importance for UPSC mains
- prelim_fact: (optional) a single-line fact for prelims if applicable, else empty string

Article Title: {title}

Article text (first 4000 chars):
{text[:4000]}

Requirements:
- Be concise and exam-focused.
- Keep each field short (context <=2 sentences, key_points each <=20 words, significance <=1 sentence).
- Output valid JSON only.
"""
    try:
        resp = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[{'role':'user', 'content': prompt}],
            max_tokens=400,
            temperature=0.0,
        )
        out = resp['choices'][0]['message']['content'].strip()
        return out
    except Exception as ex:
        print('OpenAI call failed', ex)
        return None

# ---------- Build PDF ----------
def build_pdf(structured_items, pdf_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.units import mm
    import datetime
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleLarge', fontSize=16, leading=18, spaceAfter=6, spaceBefore=6))
    styles.add(ParagraphStyle(name='Section', fontSize=12, leading=14, spaceAfter=4, spaceBefore=6))
    styles.add(ParagraphStyle(name='Body', fontSize=10, leading=12))
    styles.add(ParagraphStyle(name='Small', fontSize=9, leading=11))

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    content = []
    today = datetime.datetime.now().strftime('%d %B %Y')
    content.append(Paragraph(f'UPSC CURRENT AFFAIRS – {today}', styles['TitleLarge']))
    content.append(Paragraph('Auto-generated, AI-powered — concise exam-focused insights', styles['Small']))
    content.append(Spacer(1,8))

    # Group by category order: GS2, GS3, GS1, GS4, CME, FFP, Mapping, Misc
    order = ['GS2','GS3','GS1','GS4','CME','FFP','Mapping','Misc']
    for cat in order:
        items = [i for i in structured_items if i.get('category')==cat]
        if not items:
            continue
        content.append(Paragraph(cat, styles['Section']))
        for it in items:
            content.append(Paragraph(f"<b>{it.get('section_heading')}</b>", styles['Body']))
            if it.get('context'):
                content.append(Paragraph(f"Context: {it.get('context')}", styles['Body']))
            kp = it.get('key_points','')
            if kp:
                for point in kp if isinstance(kp, list) else [kp]:
                    content.append(Paragraph(f"- {point}", styles['Body']))
            if it.get('significance'):
                content.append(Paragraph(f"Significance: {it.get('significance')}", styles['Body']))
            if it.get('prelim_fact'):
                content.append(Paragraph(f"Facts for Prelims: {it.get('prelim_fact')}", styles['Body']))
            content.append(Spacer(1,6))
        content.append(Spacer(1,8))

    content.append(Paragraph('Note: Summaries auto-generated. Verify with original sources and PIB for official data.', styles['Small']))
    doc.build(content)

# ---------- Email ----------
def email_pdf(pdf_path):
    SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
    SMTP_USER = os.environ.get('SMTP_USER')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
    EMAIL_TO = os.environ.get('EMAIL_TO')

    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        raise EnvironmentError('Missing SMTP_USER/SMTP_PASSWORD/EMAIL_TO')

    msg = EmailMessage()
    msg['From'] = SMTP_USER
    msg['To'] = EMAIL_TO
    msg['Subject'] = 'UPSC Current Affairs — AI Brief (' + datetime.datetime.now().strftime('%d %b %Y') + ')'
    msg.set_content('Attached: AI-generated UPSC daily brief.')

    with open(pdf_path, 'rb') as f:
        data = f.read()
    msg.add_attachment(data, maintype='application', subtype='pdf', filename=os.path.basename(pdf_path))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print('Email sent to', EMAIL_TO)

# ---------- Main flow ----------
def main():
    openai_key = os.environ.get('OPENAI_API_KEY')
    entries = fetch_feed_entries()
    selected = []
    seen = set()
    for e in entries:
        if e['link'] in seen: continue
        seen.add(e['link'])
        selected.append(e)
        if len(selected) >= MAX_ARTICLES: break

    structured = []
    for e in selected:
        print('Processing:', e['title'])
        text = extract_article_text(e['link'])
        if not text:
            print('No text extracted; skipping article.')
            continue
        json_out = None
        if openai_key:
            json_out = openai_classify_and_summarize(openai_key, e['title'], text)
        if not json_out:
            # fallback: simple mapping and extractive summary
            from textwrap import shorten
            cat = 'Misc'
            heading = e['title'][:120]
            context = shorten(text.replace('\n',' '), width=180)
            key_points = [shorten(p, width=120) for p in (text.split('.')[:3]) if p.strip()]
            significance = ''
            prelim_fact = ''
            structured.append({
                'category': cat, 'section_heading': heading, 'context': context,
                'key_points': key_points, 'significance': significance, 'prelim_fact': prelim_fact
            })
            continue
        # parse JSON (OpenAI output expected to be JSON)
        import json
        try:
            parsed = json.loads(json_out)
        except Exception as ex:
            print('Failed parse JSON from model; raw output saved.')
            parsed = {'category':'Misc','section_heading':e['title'],'context':'','key_points':[], 'significance':'','prelim_fact':''}
        # normalize key_points to list
        kp = parsed.get('key_points', [])
        if isinstance(kp, str):
            # split on line breaks or ';' or '.' heuristics
            kp = [k.strip() for k in kp.replace('\r','\n').split('\n') if k.strip()][:4]
        structured.append({
            'category': parsed.get('category','Misc'),
            'section_heading': parsed.get('section_heading', e['title']),
            'context': parsed.get('context',''),
            'key_points': kp,
            'significance': parsed.get('significance',''),
            'prelim_fact': parsed.get('prelim_fact','')
        })
        time.sleep(1)

    pdf_name = os.environ.get('PDF_PREFIX','UPSC_current_affairs_ai_') + get_today_str() + '.pdf'
    pdf_path = os.path.join(os.getcwd(), pdf_name)
    build_pdf(structured, pdf_path)
    print('PDF generated at', pdf_path)
    email_pdf(pdf_path)

if __name__ == '__main__':
    main()
