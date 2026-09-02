#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LIZ LISA (tokyokawaiilife.jp) 卖空/补货/上新监控脚本
- 解析商品页 SKU 卖空状态
- 支持卖空通知 + 补货通知 + 上新通知
- 分批扫描防限流 + 429 自动退避重试
- 状态合并保存 + 重建期不刷屏
- 邮件 + 微信双通道通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
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
MAX_WORKERS = 5         # 并发抓取数（调低防限流）
MAX_PER_RUN = 100       # 每轮最多扫描的商品数（分批防限流）
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/monitor_state.json")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")

# ======================== 通知 ========================
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
    soldout_events = [e for e in events if e["type"] == "SOLD_OUT"]
    restock_events = [e for e in events if e["type"] == "RESTOCK"]
    new_events = [e for e in events if e["type"] == "NEW"]
    if new_events:
        _notify_new(new_events)
    if soldout_events:
        _notify_soldout(soldout_events)
    if restock_events:
        _notify_restock(restock_events)

def _notify_soldout(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 [lizlisa] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[lizlisa]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [lizlisa] {len(events)} 个SKU卖空"
        title = f"[lizlisa]{len(events)}个SKU卖空"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [lizlisa] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [lizlisa] 卖空告警\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)

def _notify_restock(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📦 [lizlisa] 补货: {e['product_name']} - {e['sku']}"
        title = f"[lizlisa]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [lizlisa] {len(events)} 个SKU补货"
        title = f"[lizlisa]{len(events)}个SKU补货"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#27ae60;">📦 [lizlisa] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [lizlisa] 补货通知\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)

def _notify_new(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🆕 [lizlisa] 上新: {e['product_name']}"
        title = f"[lizlisa]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [lizlisa] {len(events)} 个商品上新"
        title = f"[lizlisa]{len(events)}个上新"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e67e22;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [lizlisa] 上新通知</h2><p>{len(events)} 个新商品上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [lizlisa] 上新通知\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
# ======================== 抓取与解析 ========================
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
    # 提取商品主图（模特图）
    image = ""
    img_m = re.search(r'https://lizlisaadmin\.fs-storage\.jp/fs2cabinet/[^"\'<>\s]*-m-01-ds\.[^"\'<>\s]+', html)
    if img_m:
        image = img_m.group(0).replace("-ds.", "-pl.")
    # 提取每个颜色对应的图片（s-type 图片的 alt 属性包含颜色名）
    color_images = {}
    for m in re.finditer(r'<img[^>]*src="(https://lizlisaadmin\.fs-storage\.jp/fs2cabinet/[^"\'<>]*-s-\d+-ds\.jpg)"[^>]*alt="([^"]+)"', html):
        img_url = m.group(1)
        color_name = m.group(2).strip()
        if color_name and color_name not in color_images:
            color_images[color_name] = img_url.replace("-ds.", "-pl.")
    return {"name": name, "number": num, "image": image, "color_images": color_images, "skus": skus}

def fetch_page(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.9"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    try:
        return raw.decode("Shift_JIS")
    except:
        return raw.decode("utf-8", errors="replace")

def fetch_page_with_retry(url, retries=4, base_delay=5):
    """带 429 退避重试的抓取"""
    for attempt in range(1, retries + 1):
        try:
            return fetch_page(url)
        except HTTPError as e:
            if e.code == 429 and attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"429 限流，{delay}s 后重试 ({attempt}/{retries}): {url}")
                time.sleep(delay)
                continue
            raise
        except Exception:
            if attempt < retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise

def discover_product_urls():
    product_ids = set()
    for cat_url in CATEGORY_URLS:
        page = 1
        while True:
            url = cat_url if page == 1 else f"{cat_url}/1/{page}"
            try:
                html = fetch_page_with_retry(url)
                ids = re.findall(r'lizlisaadmin/[a-z_-]+/(\d+-\d+-\d+)', html)
                if not ids:
                    break
                before = len(product_ids)
                product_ids.update(ids)
                if len(product_ids) == before and page > 1:
                    break
                pager = re.search(r'(\d+) 件中.*?(\d+)-(\d+) 件表示', html)
                if pager:
                    if int(pager.group(3)) >= int(pager.group(1)):
                        break
                else:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"抓取分类页失败: {url} → {e}")
                break
    urls = [PRODUCT_URL_TEMPLATE.format(pid) for pid in sorted(product_ids)]
    logger.info(f"发现 {len(urls)} 个商品")
    return urls

# ======================== 状态管理 ========================
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
# ======================== 主逻辑 ========================
def check_product(url):
    try:
        return url, parse_product(fetch_page_with_retry(url))
    except Exception as e:
        logger.warning(f"抓取失败: {url} → {e}")
        return url, None

def main():
    if "--test-notify" in sys.argv:
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "ピンク(110)", "number": "262-6230-0", "url": "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6230-0", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "ブラック(104)", "number": "262-6230-0", "url": "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6230-0", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试-上新】", "sku": "ホワイト(104)", "number": "262-6230-0", "url": "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6230-0", "time": datetime.now().isoformat()},
        ])
        return

    product_urls = discover_product_urls()
    if not product_urls:
        logger.warning("未获取到商品列表，本轮跳过，保留旧状态")
        return

    # 分批扫描：每轮只扫一部分，从上次停下的位置继续
    prev = load_state()
    all_urls = sorted(product_urls)
    cursor = prev.get("_cursor", 0)
    if cursor >= len(all_urls):
        cursor = 0
    batch = all_urls[cursor:cursor + MAX_PER_RUN]
    next_cursor = cursor + len(batch)
    if next_cursor >= len(all_urls):
        next_cursor = 0
    logger.info(f"lizlisa 监控启动: 总 {len(all_urls)} 个，本轮扫 {len(batch)} 个 (从第 {cursor} 个开始)")

    # 上新检测：维护一个独立的"已见过商品"集合（与分批游标无关）
    seen_urls = set(prev.get("_seen_urls", []))
    # 双保险：如果 _seen_urls 为空，但 prev 里有旧商品，用旧商品 URL 初始化
    if not seen_urls:
        seen_urls = {k for k in prev.keys() if not k.startswith("_")}
        if seen_urls:
            logger.info(f"从旧状态恢复 {len(seen_urls)} 个已见商品")

    # 重建检测：基于实际有 SKU 状态的商品数，而不是 _seen_urls
    products_with_skus = len([k for k in prev.keys() if not k.startswith("_")])

    new_state = {}
    events = []

    # ===== 全局新品检测：立即处理，不受 cursor 影响 =====
    new_product_urls = set(all_urls) - seen_urls
    if new_product_urls:
        logger.info(f"发现 {len(new_product_urls)} 个新商品链接，立即处理")
        for url in sorted(new_product_urls)[:30]:  # 每轮最多处理30个，防限流
            try:
                info = parse_product(fetch_page_with_retry(url))
                if not info or not info.get("skus"):
                    continue
                cur = {s["name"]: s["sold_out"] for s in info["skus"]}
                if not cur:
                    continue
                new_state[url] = {"name": info["name"], "skus": cur}
                seen_urls.add(url)
                # 上新通知
                first_sku = next(iter(cur.keys()), "")
                color_images = info.get("color_images", {})
                sku_image = color_images.get(first_sku, "") or info.get("image", "")
                events.append({"type": "NEW", "product_name": info["name"], "sku": first_sku, "number": info.get("number", ""), "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                logger.info(f"🆕 上新: {info['name']}")
            except Exception as e:
                logger.warning(f"新品处理失败: {url} → {e}")

    # ===== 正常分批扫描（卖空/补货）=====
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_product, u): u for u in batch}
        for f in as_completed(futures):
            url, info = f.result()
            if not info:
                # 抓取失败，保留旧状态
                if url in prev:
                    new_state[url] = prev[url]
                    logger.info(f"抓取失败，保留旧状态: {url}")
                continue
            # 跳过已经在全局新品处理里加过的
            if url in new_state:
                continue
            cur = {s["name"]: s["sold_out"] for s in info["skus"]}
            if not cur:
                continue
            new_state[url] = {"name": info["name"], "skus": cur}
            color_images = info.get("color_images", {})
            prev_skus = prev.get(url, {}).get("skus", {})
            for sn, so in cur.items():
                was = prev_skus.get(sn, False)
                # 优先用颜色对应图片，没有就退回主图
                sku_image = color_images.get(sn, "") or info.get("image", "")
                if so and not was:
                    events.append({"type": "SOLD_OUT", "product_name": info["name"], "sku": sn, "number": info.get("number", ""), "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {info['name']} - {sn}")
                elif not so and was:
                    events.append({"type": "RESTOCK", "product_name": info["name"], "sku": sn, "number": info.get("number", ""), "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {info['name']} - {sn}")

    logger.info(f"本轮扫描完成: {len(new_state)} 个商品, {len(events)} 个变化")

    # 如果还没完整记录所有商品的 SKU 状态，只推上新，不推卖空/补货（避免重建时刷屏）
    is_rebuilding = products_with_skus < len(all_urls)
    if is_rebuilding:
        logger.info(f"状态重建中，本轮不推送卖空/补货（已有 SKU 状态 {products_with_skus}/{len(all_urls)} 个商品）")
        new_events = [e for e in events if e["type"] == "NEW"]
        if new_events:
            notify_events(new_events)
        else:
            logger.info("本轮无变化")
    else:
        if events:
            notify_events(events)
        else:
            logger.info("本轮无变化")

    # 合并旧状态：保留未扫描商品的 SKU 数据，只更新本轮扫过的
    final_state = dict(prev)
    final_state.update(new_state)
    final_state["_cursor"] = next_cursor
    final_state["_seen_urls"] = sorted(seen_urls)
    save_state(final_state)
    logger.info(f"状态已保存（下一轮从第 {next_cursor} 个开始，已记录 {len(seen_urls)} 个商品）")

if __name__ == "__main__":
    main()
