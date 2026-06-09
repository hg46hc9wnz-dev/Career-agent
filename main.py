import os
import re
import sqlite3
import hashlib
import smtplib
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
DB = 'agent.db'
HEADERS = {'User-Agent': 'Mozilla/5.0 (CareerPageAgent/1.0)'}


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'auto', active INTEGER NOT NULL DEFAULT 1)")
        c.execute("CREATE TABLE IF NOT EXISTS filters (id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT NOT NULL, location_contains TEXT DEFAULT '', UNIQUE(keyword, location_contains))")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS seen_jobs (job_key TEXT PRIMARY KEY, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS recent_results (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, title TEXT, location TEXT, url TEXT, source_url TEXT, matched_filter TEXT, scanned_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.commit()


def q(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        rows = cur.fetchall()
        c.commit()
        return [dict(r) for r in rows]


def execute(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur.rowcount


def get_settings():
    rows = q('SELECT key, value FROM settings')
    return {r['key']: r['value'] for r in rows}


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def detect_kind(url):
    u = url.lower()
    if 'greenhouse.io' in u or 'boards.greenhouse.io' in u:
        return 'greenhouse'
    if 'lever.co' in u or 'jobs.lever.co' in u:
        return 'lever'
    return 'generic'


def http_get(url):
    return requests.get(url, headers=HEADERS, timeout=25)


def scrape_greenhouse(company, url):
    token = None
    m = re.search(r'boards\\.greenhouse\\.io/([^/?#]+)', url)
    if m:
        token = m.group(1)
    else:
        text = http_get(url).text
        m = re.search(r'boards\\.greenhouse\\.io/([^\"\\'\\s<>]+)', text)
        if m:
            token = m.group(1)
    if not token:
        return scrape_generic(company, url)
    api = f'https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true'
    data = http_get(api).json()
    jobs = []
    for item in data.get('jobs', []):
        jobs.append({
            'company': company,
            'title': item.get('title', '').strip(),
            'location': ((item.get('location') or {}).get('name') or '').strip(),
            'url': item.get('absolute_url', '').strip(),
            'source_url': url,
            'description': item.get('content', '') or ''
        })
    return jobs


def scrape_lever(company, url):
    slug = None
    m = re.search(r'jobs\\.lever\\.co/([^/?#]+)', url)
    if m:
        slug = m.group(1)
    else:
        text = http_get(url).text
        m = re.search(r'api\\.lever\\.co/v0/postings/([^/?#]+)', text)
        if m:
            slug = m.group(1)
    if not slug:
        return scrape_generic(company, url)
    api = f'https://api.lever.co/v0/postings/{slug}?mode=json'
    data = http_get(api).json()
    jobs = []
    for item in data:
        jobs.append({
            'company': company,
            'title': item.get('text', '').strip(),
            'location': ((item.get('categories') or {}).get('location') or '').strip(),
            'url': item.get('hostedUrl', '').strip(),
            'source_url': url,
            'description': item.get('descriptionPlain', '') or item.get('description', '') or ''
        })
    return jobs


def scrape_generic(company, url):
    html = http_get(url).text
    soup = BeautifulSoup(html, 'html.parser')
    jobs = []
    seen = set()
    for a in soup.find_all('a', href=True):
        title = ' '.join(a.get_text(' ', strip=True).split())
        href = urljoin(url, a['href'])
        label = f'{title} {href}'.lower()
        if not title:
            continue
        if any(k in label for k in ['job', 'career', 'position', 'opening', 'vacanc', 'apply']):
            key = (title.lower(), href)
            if key in seen:
                continue
            seen.add(key)
            jobs.append({'company': company, 'title': title, 'location': '', 'url': href, 'source_url': url, 'description': ''})
    return jobs


def scrape_target(company, url, kind='auto'):
    kind = detect_kind(url) if kind == 'auto' else kind
    if kind == 'greenhouse':
        return scrape_greenhouse(company, url)
    if kind == 'lever':
        return scrape_lever(company, url)
    return scrape_generic(company, url)


def norm(s):
    return ' '.join((s or '').lower().split())


def match_jobs(jobs, filters):
    matched = []
    for job in jobs:
        hay = f"{job.get('title', '')} {job.get('location', '')} {job.get('description', '')}".lower()
        for f in filters:
            kw = norm(f['keyword'])
            loc = norm(f['location_contains'])
            if kw and kw in hay:
                if not loc or loc in norm(job.get('location', '')) or loc in hay:
                    matched.append((job, f"{f['keyword']} | {f['location_contains'] or 'anywhere'}"))
                    break
    return matched


def mark_seen(job):
    key = hashlib.sha256(f"{job['company']}|{job['title']}|{job['location']}|{job['url']}".encode()).hexdigest()
    return execute('INSERT OR IGNORE INTO seen_jobs(job_key) VALUES (?)', (key,)) == 1


def add_recent(job, matched_filter):
    execute('INSERT INTO recent_results(company, title, location, url, source_url, matched_filter) VALUES (?, ?, ?, ?, ?, ?)', (job['company'], job['title'], job['location'], job['url'], job['source_url'], matched_filter))


def render_email(rows):
    body = []
    for r in rows:
        body.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee'>{escape(r['company'])}</td><td style='padding:8px;border-bottom:1px solid #eee'><a href='{escape(r['url'])}'>{escape(r['title'])}</a></td><td style='padding:8px;border-bottom:1px solid #eee'>{escape(r['location'] or '—')}</td><td style='padding:8px;border-bottom:1px solid #eee'><a href='{escape(r['source_url'])}'>Career page</a></td></tr>")
    rows_html = ''.join(body) if body else "<tr><td colspan='4' style='padding:8px'>No matching jobs found today.</td></tr>"
    return f"<html><body style='font-family:Segoe UI,Arial,sans-serif'><h2>Daily Career Page Digest</h2><table style='border-collapse:collapse;width:100%'><thead><tr><th align='left' style='padding:8px;border-bottom:2px solid #ccc'>Company</th><th align='left' style='padding:8px;border-bottom:2px solid #ccc'>Job title</th><th align='left' style='padding:8px;border-bottom:2px solid #ccc'>Location</th><th align='left' style='padding:8px;border-bottom:2px solid #ccc'>Links</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>"


def send_digest(rows):
    s = get_settings()
    host = s.get('SMTP_HOST') or os.getenv('SMTP_HOST')
    port = int(s.get('SMTP_PORT') or os.getenv('SMTP_PORT') or 587)
    username = s.get('SMTP_USERNAME') or os.getenv('SMTP_USERNAME')
    password = s.get('SMTP_PASSWORD') or os.getenv('SMTP_PASSWORD')
    email_from = s.get('EMAIL_FROM') or os.getenv('EMAIL_FROM')
    email_to = s.get('EMAIL_TO') or os.getenv('EMAIL_TO')
    if not all([host, username, password, email_from, email_to]):
        raise ValueError('Email settings are incomplete')
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Career Page Digest — {len(rows)} matching jobs"
    msg['From'] = email_from
    msg['To'] = email_to
    msg.attach(MIMEText(render_email(rows), 'html'))
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.sendmail(email_from, [email_to], msg.as_string())


def run_scan(send_email=True, only_new=True):
    targets = q('SELECT * FROM targets WHERE active=1 ORDER BY company, url')
    filters = q('SELECT * FROM filters ORDER BY keyword, location_contains')
    jobs = []
    errors = []
    for t in targets:
        try:
            jobs.extend(scrape_target(t['company'], t['url'], t['kind']))
        except Exception as e:
            errors.append(f"{t['company']} ({t['url']}): {e}")
    results = []
    for job, matched_filter in match_jobs(jobs, filters):
        is_new = mark_seen(job)
        if is_new or not only_new:
            results.append(job)
            add_recent(job, matched_filter)
    if send_email:
        send_digest(results)
    return {'targets_scanned': len(targets), 'matched_count': len(results), 'errors': errors}


init_db()
st.set_page_config(page_title='Career Page Agent', page_icon='📌', layout='wide')
st.title('📌 Career Page Agent')
st.caption('Track company career pages, match roles, and email yourself a daily digest.')

t1, t2, t3, t4 = st.tabs(['Targets', 'Role filters', 'Run now', 'Settings'])
with t1:
    st.subheader('Career pages')
    with st.form('targets_form', clear_on_submit=True):
        company = st.text_input('Company')
        url = st.text_input('Career page URL')
        kind = st.selectbox('Type', ['auto', 'greenhouse', 'lever', 'generic'])
        active = st.checkbox('Active', value=True)
        if st.form_submit_button('Add / update') and company and url:
            execute("INSERT INTO targets(company, url, kind, active) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET company=excluded.company, kind=excluded.kind, active=excluded.active", (company.strip(), url.strip(), kind, 1 if active else 0))
            st.success('Saved')
    rows = q('SELECT * FROM targets ORDER BY company, url')
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        choices = {f"{r['company']} — {r['url']}": r['id'] for r in rows}
        pick = st.selectbox('Delete target', [''] + list(choices.keys()))
        if st.button('Delete selected target') and pick:
            execute('DELETE FROM targets WHERE id=?', (choices[pick],))
            st.rerun()
with t2:
    st.subheader('Role filters')
    with st.form('filters_form', clear_on_submit=True):
        keyword = st.text_input('Keyword (for example: data analyst)')
        location = st.text_input('Location contains (optional)')
        if st.form_submit_button('Add / update') and keyword:
            execute('INSERT OR IGNORE INTO filters(keyword, location_contains) VALUES (?, ?)', (keyword.strip(), location.strip()))
            st.success('Saved')
    rows = q('SELECT * FROM filters ORDER BY keyword, location_contains')
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        choices = {f"{r['keyword']} | {r['location_contains'] or 'anywhere'}": r['id'] for r in rows}
        pick = st.selectbox('Delete filter', [''] + list(choices.keys()))
        if st.button('Delete selected filter') and pick:
            execute('DELETE FROM filters WHERE id=?', (choices[pick],))
            st.rerun()
with t3:
    st.subheader('Run scan now')
    send_email_now = st.checkbox('Send email after scan', value=True)
    only_new = st.checkbox('Only email new jobs', value=True)
    if st.button('Run scan', type='primary'):
        summary = run_scan(send_email=send_email_now, only_new=only_new)
        st.success(f"Scanned {summary['targets_scanned']} targets and matched {summary['matched_count']} jobs.")
        if summary['errors']:
            st.warning('Some targets failed')
            st.code('\n'.join(summary['errors']))
    rows = q('SELECT * FROM recent_results ORDER BY scanned_at DESC, id DESC LIMIT 100')
    if rows:
        st.subheader('Recent results')
        st.dataframe(rows, use_container_width=True, hide_index=True)
with t4:
    st.subheader('Email settings')
    s = get_settings()
    with st.form('settings_form'):
        fields = {
            'SMTP_HOST': st.text_input('SMTP host', value=s.get('SMTP_HOST', 'smtp.office365.com')),
            'SMTP_PORT': st.text_input('SMTP port', value=s.get('SMTP_PORT', '587')),
            'SMTP_USERNAME': st.text_input('SMTP username', value=s.get('SMTP_USERNAME', '')),
            'SMTP_PASSWORD': st.text_input('SMTP password', value=s.get('SMTP_PASSWORD', ''), type='password'),
            'EMAIL_FROM': st.text_input('Email from', value=s.get('EMAIL_FROM', '')),
            'EMAIL_TO': st.text_input('Email to', value=s.get('EMAIL_TO', '')),
            'DAILY_SEND_TIME': st.text_input('Daily send time (HH:MM)', value=s.get('DAILY_SEND_TIME', '08:30')),
        }
        if st.form_submit_button('Save settings'):
            for k, v in fields.items():
                set_setting(k, v)
            st.success('Saved')
