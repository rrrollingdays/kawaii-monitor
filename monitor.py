#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os, re, json, smtplib, logging, ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

CATEGORY_URLS = [
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-items",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-dresses",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-tops",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-bottoms",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-outers",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-shoes",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-bags",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/c/all-goods",
]
PRODUCT_URL_TEMPLATE = "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/{}"
SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
MAX_WORKERS = 15
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
STATE_FILE = "/tmp/monitor_state.json"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")

def send_email(subject, body_html):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER; msg["To"] = EMAIL_TO; msg["Subject"] = subject
    msg.attach(MIMEText(re.sub(r'<[^>]+>', '', body_html), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    for port in [587, 465]:
        try:
            if port == 587:
                with smtplib.SMTP(SMTP_SERVER, 587, timeout=15) as s:
                    s.ehlo(); s.starttls(context=ctx); s.ehlo()
                    s.login(SMTP_USER, SMTP_PASSWORD)
                    s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
            else:
                with smtplib.SMTP_SSL(SMTP_SERVER, 465, context=ctx, timeout=15) as s:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                    s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
            logger.info(f"邮件已发送: {subject}")
            return True
        except Exception as e:
            logger.debug(f"端口{port}失败: {e}")
    logger.error("邮件发送失败")
    return False

def send_wechat(title, desp):
    if not SERVERCHAN_KEY:
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    data = urlencode({"title": title[:32], "desp": desp}).encode("utf-8")
    try:
        with urlopen(Request(url, data=data, method="POST"), timeout=10) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            if r.get("code") == 0:
                logger.info(f"微信推送已发送: {title}")
                return True
            logger.error(f"微信推送失败: {r.get('message')}")
    except Exception as e:
        logger.error(f"微信推送失败: {e}")
    return False

def notify_events(events):
    if not events:
        return
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 卖空告警: {e['product_name']} - {e['sku']}"
        title = f"卖空: {e['product_name'][:20]}"
    else:
        subject = f"🚨 {len(events)} 个SKU卖空告警"
        title = f"{len(events)}个SKU卖空"
    rows = ""
    for e in events:
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;">{e["time"][:19]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = ""
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n\n"
    send_email(subject, body)
    send_wechat(title, desp)

def parse_product(html):
    m = re.search(r'<h1[^>]*itemTitle[^>]*>(.*?)</h1>', html, re.DOTALL)
    name = m.group(1).strip() if m else ""
    m2 = re.search(r'商品番号\s*　\s*(.*?)</p>', html, re.DOTALL)
    num = m2.group(1).strip() if m2 else ""
    m3 = re.search(r'FS2_additional_image_tableVariation[^>]*>(.*?)</div>', html, re.DOTALL)
    tbl = m3.group(1) if m3 else ""
    skus = []
    for tr in re.finditer(r'<tr[^>]*>(.*?)</tr>', tbl, re.DOTALL):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr.group(1), re.DOTALL)
        if len(tds) < 2:
            continue
        raw = tds[0].strip()
        clean = re.sub(r'<[^>]+>', '', raw).strip()
        if not clean:
            continue
        sold = bool(re.search(r'SOLD\s*OUT', raw, re.IGNORECASE))
        clean = re.sub(r'\s*/?\s*SOLD\s*OUT\s*$', '', clean, flags=re.IGNORECASE).strip()
        skus.append({"name": clean, "sold_out": sold})
    return {"name": name, "number": num, "skus": skus}

def fetch_page(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.9"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    try:
        return raw.decode("Shift_JIS")
    except:
        return raw.decode("utf-8", errors="replace")

def discover_product_urls():
    product_ids = set()
    for cat_url in CATEGORY_URLS:
        page = 1
        while True:
            url = cat_url if page == 1 else f"{cat_url}/1/{page}"
            try:
                html = fetch_page(url)
                ids = re.findall(r'lizlisaadmin/[a-z_-]+/(\d+-\d+-\d+)', html)
                if not ids:
                    break
                before = len(product_ids)
                product_ids.update(ids)
                if len page > 1
