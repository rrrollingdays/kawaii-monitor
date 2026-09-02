#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
piumofficial.com 卖空/补货/上新监控脚本
- Shopify 平台，通过 collection JSON 接口一次性获取全部商品
- 每轮 3 个请求覆盖全部商品（旧版 107 个请求只覆盖 15%）
- 通过 variant 的 available 字段判断是否卖空
- 支持卖空通知 + 补货通知 + 上新通知
- 邮件 + 微信双通道通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, unquote

# ======================== 配置 ========================
BASE_URL = "https://piumofficial.com"
COLLECTION_JSON_URL = "https://piumofficial.com/collections/all-items/products.json"
PAGE_SIZE = 250        # Shopify 单页最大值
MAX_PAGES = 5          # 最多翻 5 页（1250 个商品，足够）
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/pium_monitor_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("pium-monitor")

# ======================== 网络请求 ========================
def fetch_url(url):
    """抓取 URL 返回文本"""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "application/json,*/*"
    })
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")

def fetch_url_with_retry(url, retries=3, base_delay=2):
    """带 429 退避重试的抓取"""
    for attempt in range(1, retries + 1):
        try:
            return fetch_url(url)
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

def fetch_json(url):
    """抓取 JSON 接口"""
    text = fetch_url_with_retry(url)
    return json.loads(text)

def clean_img_url(src):
    """清理图片 URL：确保 https、清理 HTML 实体"""
    if not src:
        return ""
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("http://"):
        src = "https://" + src[7:]
    return src.replace("&", "&").split("&width")[0].split("?width")[0]

# ======================== 商品获取 ========================
def fetch_all_products():
    """
    用 collection JSON 接口一次性获取全部商品（含 SKU 卖空状态）
    每页 250 个，669 个商品只需 3 个请求
    """
    products = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{COLLECTION_JSON_URL}?limit={PAGE_SIZE}&page={page}"
        try:
            data = fetch_json(url)
        except Exception as e:
            logger.warning(f"列表第 {page} 页抓取失败: {e}")
            if page == 1:
                # 首页失败，本轮数据不可信，作废
                raise
            break  # 中间页失败，用已拿到的部分（不会误报）
        prods = data.get("products", [])
        if not prods:
            break
        products.extend(prods)
        logger.info(f"第 {page} 页: {len(prods)} 个商品 (累计 {len(products)})")
        if len(prods) < PAGE_SIZE:
            break
    return products
# ======================== 通知 ========================
def send_email(subject, body_html):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        logger.warning("邮件配置不完整，跳过")
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
        logger.warning("Server酱未配置，跳过")
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
        subject = f"🚨 [pium] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[pium]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [pium] {len(events)} 个SKU卖空"
        title = f"[pium]{len(events)}个SKU卖空"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [pium] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [pium] 卖空告警\n\n"
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
        subject = f"📦 [pium] 补货: {e['product_name']} - {e['sku']}"
        title = f"[pium]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [pium] {len(events)} 个SKU补货"
        title = f"[pium]{len(events)}个SKU补货"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#27ae60;">📦 [pium] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [pium] 补货通知\n\n"
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
        subject = f"🆕 [pium] 上新: {e['product_name']}"
        title = f"[pium]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [pium] {len(events)} 个商品上新"
        title = f"[pium]{len(events)}个上新"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e67e22;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [pium] 上新通知</h2><p>{len(events)} 个新商品上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [pium] 上新通知\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
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
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "グレー / Free", "number": "1026a070301181", "url": "https://piumofficial.com/products/test", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "ブラック / Free", "number": "1026a070301181", "url": "https://piumofficial.com/products/test", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试-上新】", "sku": "ピンク / Free", "number": "1026a070301181", "url": "https://piumofficial.com/products/test", "time": datetime.now().isoformat()},
        ])
        return

    # 一次拿全部商品（含 SKU 卖空状态）
    try:
        products = fetch_all_products()
    except Exception as e:
        logger.warning(f"获取商品列表失败: {e}，本轮跳过，保留旧状态")
        return
    if not products:
        logger.warning("未获取到商品列表，本轮跳过，保留旧状态")
        return

    prev = load_state()

    # 上新检测：维护一个独立的"已见过商品"集合
    seen_urls = set(prev.get("_seen_urls", []))
    # 双保险：如果 _seen_urls 为空，但 prev 里有旧商品，用旧商品 URL 初始化
    if not seen_urls:
        seen_urls = {k for k in prev.keys() if not k.startswith("_")}
        if seen_urls:
            logger.info(f"从旧状态恢复 {len(seen_urls)} 个已见商品")

    # 首次运行判断：没有任何商品的 SKU 状态 → 建基线，不发通知
    products_with_skus = len([k for k in prev.keys() if not k.startswith("_")])
    first_run = products_with_skus == 0
    if first_run:
        logger.info("首次运行：建立基线状态，本轮不发通知")
    else:
        logger.info(f"pium 监控启动: 本轮全量扫描 {len(products)} 个商品 (已有状态 {products_with_skus} 个)")

    new_state = {}
    events = []

    for p in products:
        handle = p.get("handle", "")
        if not handle:
            continue
        url = f"{BASE_URL}/products/{handle}"

        # 解析全部 SKU 状态（JSON 的 available 字段）
        variants = p.get("variants", [])
        cur = {}
        img_by_name = {}
        for v in variants:
            vname = v.get("title") or v.get("option1") or str(v.get("id", ""))
            if not vname:
                continue
            if "available" in v:
                cur[vname] = not v["available"]
            fi = v.get("featured_image") or {}
            src = clean_img_url(fi.get("src", ""))
            if src:
                img_by_name[vname] = src
        if not cur:
            continue

        name = p.get("title", handle)
        new_state[url] = {"name": name, "skus": cur}

        # 主图
        images = p.get("images", [])
        main_image = clean_img_url(images[0].get("src", "")) if images else ""
        # 货号 = handle（URL解码 + 去 -copy 等后缀）
        raw_handle = unquote(handle)
        product_number = re.sub(r'-copy$|-1$|-2$', '', raw_handle)

        is_new = url not in seen_urls
        if is_new:
            seen_urls.add(url)

        # 首轮建基线：只记录状态，不做事件检测
        if first_run:
            continue

        if is_new:
            # ===== 上新 =====
            first_sku = next(iter(cur.keys()), "")
            sku_image = img_by_name.get(first_sku, "") or main_image
            events.append({"type": "NEW", "product_name": name, "sku": first_sku, "number": product_number, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
            logger.info(f"🆕 上新: {name}")
        else:
            # ===== 卖空/补货对比 =====
            prev_skus = prev.get(url, {}).get("skus", {})
            for sn, so in cur.items():
                was = prev_skus.get(sn, False)
                sku_image = img_by_name.get(sn, "") or main_image
                if so and not was:
                    events.append({"type": "SOLD_OUT", "product_name": name, "sku": sn, "number": product_number, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {name} - {sn}")
                elif not so and was:
                    events.append({"type": "RESTOCK", "product_name": name, "sku": sn, "number": product_number, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {name} - {sn}")

    logger.info(f"本轮扫描完成: {len(new_state)} 个商品, {len(events)} 个变化")

    if first_run:
        logger.info(f"基线建立完成: {len(new_state)} 个商品已记录，下一轮开始正常监控")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")

    # 合并旧状态（保留 JSON 异常商品和已下架商品的旧数据，防止误报）
    final_state = dict(prev)
    final_state.update(new_state)
    final_state.pop("_cursor", None)  # 旧版遗留字段，不再需要
    final_state["_seen_urls"] = sorted(seen_urls)
    save_state(final_state)
    logger.info(f"状态已保存（已记录 {len(seen_urls)} 个商品）")

if __name__ == "__main__":
    main()
