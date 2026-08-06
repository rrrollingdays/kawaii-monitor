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

BASE_URL = "https://piumofficial.com"
COLLECTION_URL = "https://piumofficial.com/collections/all-items"
MAX_PAGES = 20
MAX_WORKERS = 10
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/pium_monitor_state.json")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("pium-monitor")

def fetch_url(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.9"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")

def fetch_json(url):
    return json.loads(fetch_url(url))

def discover_product_handles():
    handles = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{COLLECTION_URL}?page={page}"
        try:
            html = fetch_url(url)
            found = re.findall(r'href="/products/([^"?"]+)', html)
            if not found:
                logger.info(f"第 {page} 页无商品，停止翻页")
                break
            before = len(handles)
            handles.update(found)
            added = len(handles) - before
            logger.info(f"第 {page} 页: 发现 {added} 个新商品 (累计 {len(handles)})")
            if added == 0 and page > 1:
                break
        except Exception as e:
            logger.warning(f"抓取列表页失败: page={page} → {e}")
            break
    logger.info(f"共发现 {len(handles)} 个商品")
    return sorted(handles)
def parse_product(handle):
    json_url = f"{BASE_URL}/products/{handle}.json"
    try:
        data = fetch_json(json_url)
    except Exception as e:
        logger.warning(f"JSON抓取失败: {handle} → {e}")
        return parse_product_html(handle)
    product = data.get("product", {})
    if not product:
        return parse_product_html(handle)
    name = product.get("title", handle)
    variants = product.get("variants", [])
    skus = []
    need_html = True
    for v in variants:
        vtitle = v.get("title", "")
        available = v.get("available", None)
        if available is not None:
            need_html = False
        skus.append({"name": vtitle, "variant_id": v.get("id", ""), "sold_out": not available if available is not None else None})
    if need_html:
        return parse_product_html(handle, name, skus)
    image = ""
    images = product.get("images", [])
    if images:
        image = images[0].get("src", "")
    return {"name": name, "handle": handle, "url": f"{BASE_URL}/products/{handle}", "image": image, "skus": skus}

def parse_product_html(handle, name=None, json_skus=None):
    url = f"{BASE_URL}/products/{handle}"
    try:
        html = fetch_url(url)
    except Exception as e:
        logger.warning(f"HTML抓取失败: {handle} → {e}")
        return None
    if not name:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        name = m.group(1).strip() if m else handle
    vid_map = {}
    if json_skus:
        for s in json_skus:
            vid_map[s["variant_id"]] = s["name"]
    else:
        for m in re.finditer(r'data-variant-id="(\d+)"[^>]*data-option-value="([^"]*)"', html):
            vid_map[m.group(1)] = m.group(2)
        if not vid_map:
            for m in re.finditer(r'data-option-value="([^"]*)"[^>]*data-variant-id="(\d+)"', html):
                vid_map[m.group(2)] = m.group(1)
    skus = []
    forms = re.findall(r'<form[^>]*action="/cart/add"[^>]*>.*?</form>', html, re.DOTALL)
    if forms:
        for form_html in forms:
            vid_m = re.search(r'value="(\d+)"', form_html)
            title_m = re.search(r'<p>([^<]*)</p>', form_html)
            vid = vid_m.group(1) if vid_m else ""
            title = title_m.group(1).strip() if title_m else vid_map.get(vid, "")
            has_bis = "BIS_trigger" in form_html
            has_soldout = "Sold Out" in form_html or "sold-out" in form_html.lower()
            has_cart = "カートに入れる" in form_html or "cart" in form_html.lower()
            sold_out = has_bis or (has_soldout and not has_cart)
            skus.append({"name": title, "variant_id": vid, "sold_out": sold_out})
    else:
        for m in re.finditer(r'data-variant-id="(\d+)"[^>]*?data-option-value="([^"]*)"', html, re.DOTALL):
            vid, title = m.group(1), m.group(2)
            ctx_start = m.start()
            ctx_end = min(len(html), m.end() + 500)
            ctx = html[ctx_start:ctx_end]
            sold_out = "sold-out" in ctx.lower() or "Sold Out" in ctx
            skus.append({"name": title, "variant_id": vid, "sold_out": sold_out})
    if not skus and json_skus:
        skus = json_skus
    image = ""
    og_m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html)
    if og_m:
        image = og_m.group(1)
    return {"name": name, "handle": handle, "url": url, "image": image, "skus": skus}
def send_email(subject, body_html):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(re.sub(r'<[^>]+>', '', body_html), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    for port in [587, 465]:
        try:
            if port == 587:
                with smtplib.SMTP(SMTP_SERVER, 587, timeout=15) as s:
                    s.ehlo()
                    s.starttls(context=ctx)
                    s.ehlo()
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
    soldout_events = [e for e in events if e["type"] == "SOLD_OUT"]
    restock_events = [e for e in events if e["type"] == "RESTOCK"]
    if soldout_events:
        _notify_soldout(soldout_events)
    if restock_events:
        _notify_restock(restock_events)

def _notify_soldout(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 [pium] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[pium]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [pium] {len(events)} 个SKU卖空"
        title = f"[pium]{len(events)}个SKU卖空"
    rows = ""
    img_tags = ""
    for e in events:
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
        if e.get("image"):
            img_tags += f'<div style="margin:8px 0;"><img src="{e["image"]}" style="max-width:220px;border:1px solid #ddd;" loading="lazy"></div>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [pium] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table>{img_tags}</body></html>'
    desp = "### [pium] 卖空告警\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)

def _notify_restock(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📦 [pium] 补货: {e['product_name']} - {e['sku']}"
        title = f"[pium]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [pium] {len(events)} 个SKU补货"
        title = f"[pium]{len(events)}个SKU补货"
    rows = ""
    img_tags = ""
    for e in events:
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
        if e.get("image"):
            img_tags += f'<div style="margin:8px 0;"><img src="{e["image"]}" style="max-width:220px;border:1px solid #ddd;" loading="lazy"></div>'
    body = f'<html><body><h2 style="color:#27ae60;">📦 [pium] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{rows}</table>{img_tags}</body></html>'
    desp = "### [pium] 补货通知\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except:
        pass

def check_product(handle):
    try:
        return handle, parse_product(handle)
    except Exception as e:
        logger.warning(f"抓取失败: {handle} → {e}")
        return handle, None

def main():
    if "--test-notify" in sys.argv:
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "グレー / Free", "url": "https://piumofficial.com/products/test", "image": "", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "ブラック / Free", "url": "https://piumofficial.com/products/test", "image": "", "time": datetime.now().isoformat()},
        ])
        return

    handles = discover_product_handles()
    logger.info(f"pium 监控启动: {len(handles)} 个商品")
    prev = load_state()
    new_state = {}
    events = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_product, h): h for h in handles}
        for f in as_completed(futures):
            handle, info = f.result()
            if not info:
                continue
            url = info["url"]
            cur = {}
            for s in info["skus"]:
                if s.get("sold_out") is not None:
                    cur[s["name"]] = s["sold_out"]
            if not cur:
                continue
            new_state[url] = {"name": info["name"], "image": info.get("image", ""), "skus": cur}
            prev_skus = prev.get(url, {}).get("skus", {})
            for sn, so in cur.items():
                was = prev_skus.get(sn, False)
                if so and not was:
                    events.append({"type": "SOLD_OUT", "product_name": info["name"], "sku": sn, "url": url, "image": info.get("image", ""), "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {info['name']} - {sn}")
                elif not so and was:
                    events.append({"type": "RESTOCK", "product_name": info["name"], "sku": sn, "url": url, "image": info.get("image", ""), "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {info['name']} - {sn}")
    logger.info(f"扫描完成: {len(new_state)} 个商品, {sum(len(v['skus']) for v in new_state.values())} 个SKU, {len(events)} 个变化")
    if events:
        notify_events(events)
    else:
        logger.info("本轮无变化")
    save_state(new_state)
    logger.info("状态已保存")

if __name__ == "__main__":
    main()
