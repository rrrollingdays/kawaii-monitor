#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DARICH (www.darich.shop, Shopify 建站) 卖空/补货/上新/折扣监控脚本
- 走 Shopify 官方 products.json 接口，无需解析 HTML，每轮几个请求扫全站
- variant.available 字段直接给出有货/卖空，精确到颜色
- 支持卖空通知 + 补货通知 + 上新通知 + 折扣/回调通知
- 邮件 + Bark 双通道通知（微信已停用）
- 首轮运行仅建立基线，不推送任何通知（避免重建刷屏）
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

SHOP = "https://www.darich.shop"
COLLECTION_JSON = SHOP + "/collections/all/products.json?limit=250&page={page}"
PRODUCT_URL_TEMPLATE = SHOP + "/products/{}"
SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")


# —— 邮件收件人黑名单 ——
# rrrollingdays@gmail.com 不再接收任何监控邮件（其他收件人不受影响）。
# 过滤后若无人可发，退化为只发给 SMTP 发信账号自己，避免"无收件人"报错。
BLOCKED_EMAILS = {"rrrollingdays@gmail.com"}

def resolve_recipients():
    rcp = [x.strip() for x in EMAIL_TO.split(",") if x.strip()]
    rcp = [x for x in rcp if x.lower() not in BLOCKED_EMAILS]
    if not rcp and SMTP_USER:
        rcp = [SMTP_USER]
    return rcp
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
ENABLE_WECHAT = False     # 微信推送总开关（当前停用，改为 True 恢复）
BARK_URL = os.environ.get("BARK_URL", "").rstrip("/")
REQUEST_TIMEOUT = 20
PAGE_DELAY = 2            # 翻页间隔（秒），礼貌抓取
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Shopify 反爬：UA/Accept 过简会触发 429，必须用完整浏览器头
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/darich_state.json")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")

# ======================== 通知 ========================
def send_email(subject, body_html):
    recipients = resolve_recipients()
    if not SMTP_USER or not SMTP_PASSWORD or not recipients:
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER; msg["To"] = ", ".join(recipients); msg["Subject"] = subject
    msg.attach(MIMEText(re.sub(r'<[^>]+>', '', body_html), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    for attempt in range(2):
        for port in [587, 465]:
            try:
                if port == 587:
                    with smtplib.SMTP(SMTP_SERVER, 587, timeout=25) as s:
                        s.ehlo(); s.starttls(context=ctx); s.ehlo()
                        s.login(SMTP_USER, SMTP_PASSWORD)
                        s.sendmail(SMTP_USER, recipients, msg.as_string())
                else:
                    with smtplib.SMTP_SSL(SMTP_SERVER, 465, context=ctx, timeout=25) as s:
                        s.login(SMTP_USER, SMTP_PASSWORD)
                        s.sendmail(SMTP_USER, recipients, msg.as_string())
                logger.info(f"邮件已发送: {subject} | 收件人: {', '.join(recipients)}")
                return True
            except Exception as e:
                logger.warning(f"端口{port}失败: {type(e).__name__}: {e}")
        if attempt == 0:
            logger.warning("587/465 两端口均失败，8s 后整体重试")
            time.sleep(8)
    logger.error("邮件发送失败（重试后仍失败）")
    return False

def send_wechat(title, desp):
    # 微信推送已停用（Server酱）。如需恢复，把 ENABLE_WECHAT 改回 True 即可。
    if not ENABLE_WECHAT:
        return False
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
def send_bark(title, desp):
    if not BARK_URL:
        logger.warning("Bark 未配置，跳过")
        return False
    text = re.sub(r"<[^>]+>", "", desp)
    text = text.replace("**", "").replace("### ", "")[:900]
    payload = {"title": title[:60], "body": text, "group": "kawaii-monitor", "level": "timeSensitive"}
    try:
        req = Request(BARK_URL, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json; charset=utf-8"})
        with urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            if r.get("code") == 200:
                logger.info(f"Bark 推送已发送: {title}")
                return True
            logger.error(f"Bark 推送失败: {r.get('message')}")
    except Exception as e:
        logger.error(f"Bark 推送失败: {e}")
    return False

def notify_events(events):
    if not events:
        return
    soldout_events = [e for e in events if e["type"] == "SOLD_OUT"]
    restock_events = [e for e in events if e["type"] == "RESTOCK"]
    new_events = [e for e in events if e["type"] == "NEW"]
    sale_events = [e for e in events if e["type"] == "SALE"]
    priceup_events = [e for e in events if e["type"] == "PRICE_UP"]
    if new_events:
        _notify_new(new_events)
    if sale_events:
        _notify_sale(sale_events)
    if priceup_events:
        _notify_priceup(priceup_events)
    if soldout_events:
        _notify_soldout(soldout_events)
    if restock_events:
        _notify_restock(restock_events)

def _event_rows(events):
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("sku", "")}</span></td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    return rows

def _event_desp(events, heading):
    desp = f"### {heading}\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e.get('sku', '全色')}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    return desp

def _notify_sale(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📉 [darich] 折扣: {e['product_name']} -{e['discount']}"
        title = f"[darich]折扣:{e['product_name'][:15]} -{e['discount']}"
    else:
        subject = f"📉 [darich] {len(events)} 个商品限时折扣"
        title = f"[darich]{len(events)}个折扣"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;">{e.get("compare_txt", "")}</td><td style="padding:8px;border:1px solid #ddd;color:#c0392b;font-weight:bold;">¥{e["new_price"]:,}（-{e["discount"]}）</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#c0392b;">📉 [darich] 限时折扣</h2><p>{len(events)} 个商品降价:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [darich] 限时折扣\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- {e.get('compare_txt', '')}→ **¥{e['new_price']:,}**（-{e['discount']}）\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_priceup(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📈 [darich] 回调: {e['product_name']} +{e['discount']}"
        title = f"[darich]回调:{e['product_name'][:15]} +{e['discount']}"
    else:
        subject = f"📈 [darich] {len(events)} 个商品价格回调"
        title = f"[darich]{len(events)}个回调"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;">{e.get("compare_txt", "")}</td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">¥{e["new_price"]:,}（+{e["discount"]}）</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#27ae60;">📈 [darich] 价格回调</h2><p>{len(events)} 个商品涨价:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [darich] 价格回调\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- {e.get('compare_txt', '')}→ **¥{e['new_price']:,}**（+{e['discount']}）\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_soldout(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 [darich] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[darich]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [darich] {len(events)} 个SKU卖空"
        title = f"[darich]{len(events)}个SKU卖空"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [darich] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = _event_desp(events, "[darich] 卖空告警")
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_restock(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📦 [darich] 补货: {e['product_name']} - {e['sku']}"
        title = f"[darich]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [darich] {len(events)} 个SKU补货"
        title = f"[darich]{len(events)}个SKU补货"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#27ae60;">📦 [darich] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = _event_desp(events, "[darich] 补货通知")
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_new(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🆕 [darich] 上新: {e['product_name']}"
        title = f"[darich]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [darich] {len(events)} 个商品上新"
        title = f"[darich]{len(events)}个上新"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [darich] 上新通知</h2><p>{len(events)} 个新商品上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = _event_desp(events, "[darich] 上新通知")
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

# ======================== 抓取与解析 ========================
def fetch_json(url, retries=4, base_delay=5):
    """带重试的 JSON 抓取（429/5xx 退避）"""
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers=dict(BASE_HEADERS, Accept="application/json,text/plain,*/*"))
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"HTTP {e.code}，{delay}s 后重试 ({attempt}/{retries}): {url}")
                time.sleep(delay)
                continue
            raise
        except Exception:
            if attempt < retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise

def discover_products():
    """翻页拉取全站商品 JSON（每页最多 250 个）"""
    products = []
    page = 1
    while True:
        data = fetch_json(COLLECTION_JSON.format(page=page))
        batch = data.get("products", [])
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
        if page > 20:   # 保险：最多 20 页（5000 商品）
            break
        time.sleep(PAGE_DELAY)
    return products

def parse_product(p):
    """把 Shopify 商品 JSON 规整为监控内部结构"""
    skus = {}
    color_images = {}
    prices = []
    for v in p.get("variants", []):
        title = (v.get("title") or "").strip()
        if not title:
            title = (v.get("option1") or "DEFAULT").strip()
        skus[title] = bool(v.get("available", False))
        try:
            prices.append(int(float(v.get("price") or 0)))
        except (ValueError, TypeError):
            pass
        fi = v.get("featured_image") or {}
        src = fi.get("src") if isinstance(fi, dict) else None
        if src and title not in color_images:
            color_images[title] = src
    # 商品价 = 最低 variant 价；compare = 任一 variant 的 compare_at_price 中高于现价的
    price = min(prices) if prices else 0
    compare = 0
    for v in p.get("variants", []):
        try:
            c = int(float(v.get("compare_at_price") or 0))
        except (ValueError, TypeError):
            c = 0
        if c > price:
            compare = c
            break
    image = ""
    imgs = p.get("images") or []
    if imgs:
        image = imgs[0].get("src", "")
    return {
        "name": p.get("title", ""),
        "skus": skus,
        "price": price,
        "compare": compare,
        "image": image,
        "color_images": color_images,
    }

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
def main():
    if "--test-notify" in sys.argv:
        url = PRODUCT_URL_TEMPLATE.format("112638001")
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "PNK / FREE", "url": url, "image": "", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "BLK / FREE", "url": url, "image": "", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试-上新】", "sku": "WHT / FREE", "url": url, "image": "", "time": datetime.now().isoformat()},
            {"type": "SALE", "product_name": "【测试-折扣】", "sku": "全色", "url": url, "image": "", "new_price": 11550, "discount": "30%", "compare_txt": "~~¥16,500~~ →", "time": datetime.now().isoformat()},
        ])
        return

    raw_products = discover_products()
    if not raw_products:
        logger.warning("未获取到商品列表，本轮跳过，保留旧状态")
        return

    prev = load_state()
    seen = set(prev.get("_seen", []))
    # 双保险：_seen 为空但已有商品状态，从旧状态恢复
    product_states = {k: v for k, v in prev.items() if not k.startswith("_")}
    if not seen and product_states:
        seen = set(product_states.keys())
        logger.info(f"从旧状态恢复 {len(seen)} 个已见商品")

    # 首轮运行（无任何历史状态）：只建基线，不推送任何通知
    is_baseline = not product_states

    current = {}
    events = []

    for p in raw_products:
        handle = p.get("handle", "")
        if not handle:
            continue
        url = PRODUCT_URL_TEMPLATE.format(handle)
        info = parse_product(p)
        if not info["skus"]:
            continue
        current[handle] = info

        color_images = info["color_images"]
        prev_info = prev.get(handle, {})
        prev_skus = prev_info.get("skus", {}) if isinstance(prev_info, dict) else {}

        # —— 上新检测 ——
        if handle not in seen:
            seen.add(handle)
            if not is_baseline:
                first_sku = next(iter(info["skus"].keys()), "")
                sku_image = color_images.get(first_sku, "") or info["image"]
                events.append({"type": "NEW", "product_name": info["name"], "sku": first_sku, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                logger.info(f"🆕 上新: {info['name']}")

        # —— 卖空/补货检测 ——
        if not is_baseline and prev_skus:
            for sn, so in info["skus"].items():
                was = prev_skus.get(sn, False)
                sku_image = color_images.get(sn, "") or info["image"]
                if so and not was:
                    events.append({"type": "RESTOCK", "product_name": info["name"], "sku": sn, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {info['name']} - {sn}")
                elif not so and was:
                    events.append({"type": "SOLD_OUT", "product_name": info["name"], "sku": sn, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {info['name']} - {sn}")

        # —— 折扣/回调检测（对比上轮价格）——
        if not is_baseline and prev_info.get("price"):
            old_price = prev_info["price"]
            new_price = info["price"]
            if new_price and new_price != old_price:
                if new_price > old_price:
                    rise = round((new_price / old_price - 1) * 100)
                    if rise >= 2:
                        events.append({"type": "PRICE_UP", "product_name": info["name"], "sku": "全色", "url": url, "image": info["image"], "new_price": new_price, "discount": f"{rise}%", "compare_txt": f"¥{old_price:,} →", "time": datetime.now().isoformat()})
                        logger.info(f"📈 回调: {info['name']} ¥{old_price:,}→¥{new_price:,}")
                else:
                    if info["compare"] and info["compare"] > new_price:
                        base_price, compare_txt = info["compare"], f"~~¥{info['compare']:,}~~ →"
                    else:
                        base_price, compare_txt = old_price, f"¥{old_price:,} →"
                    discount = round((1 - new_price / base_price) * 100)
                    if discount >= 2:
                        events.append({"type": "SALE", "product_name": info["name"], "sku": "全色", "url": url, "image": info["image"], "new_price": new_price, "discount": f"{discount}%", "compare_txt": compare_txt, "time": datetime.now().isoformat()})
                        logger.info(f"📉 折扣: {info['name']} ¥{old_price:,}→¥{new_price:,}")

    logger.info(f"本轮扫描完成: {len(current)} 个商品, {len(events)} 个变化")

    # 抓取失败兜底：本轮没看到的商品保留旧状态（避免接口抖动导致状态丢失）
    final_state = {k: v for k, v in prev.items() if k.startswith("_")}
    for h, info in current.items():
        final_state[h] = {"name": info["name"], "skus": info["skus"], "price": info["price"], "compare": info["compare"], "image": info["image"]}
    for h, old in prev.items():
        if not h.startswith("_") and h not in current and isinstance(old, dict) and old.get("skus"):
            final_state[h] = old
            logger.info(f"本轮未扫到，保留旧状态: {old.get('name', h)}")
    final_state["_seen"] = sorted(seen)
    save_state(final_state)

    if is_baseline:
        logger.info(f"首轮基线建立完成: {len(current)} 个商品已记录，本轮不推送任何通知")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")
    logger.info(f"状态已保存（共 {len(final_state) - 1} 个商品，已见 {len(seen)} 个）")

if __name__ == "__main__":
    main()
