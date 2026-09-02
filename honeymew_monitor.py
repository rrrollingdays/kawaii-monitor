#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mycolor.jp HONEY MEW 卖空/补货/上新监控脚本
- MyColor 平台，通过 productclass2 JSON 接口一次性获取全部商品
- 每轮 1 个请求覆盖全部商品（颜色记录 × 全部尺码 SKU）
- 通过 SKU 的 web_stock_quantity 判断是否卖空（<=0 为卖空）
- 支持卖空通知 + 补货通知 + 上新通知
- 邮件 + 微信双通道通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, quote

# ======================== 配置 ========================
BASE_URL = "https://mycolor.jp/honeymew"
PRODUCTS_API = "https://mycolor.jp/api/common-proxy/HONEYMEWWeb/productclass2"
# 商品筛选条件：HONEY MEW 全部分类、排除赠品/目录/订阅等非卖品
TAGP = ("(and mycolor.product.fku "
        "(not mycolor.hiddenset.child mycolor.product.novelty mycolor.product.catalog "
        "mycolor.product.exclude_from_list mycolor.product.subscription) "
        "(or company.product.category.honeymew.tops "
        "company.product.category.honeymew.jacket-outerwear "
        "company.product.category.honeymew.bottoms "
        "company.product.category.honeymew.skirt "
        "company.product.category.honeymew.shoes "
        "company.product.category.honeymew.bag "
        "company.product.category.honeymew.onepiece "
        "company.product.category.honeymew.setup))")
PAGE_LIMIT = 500       # 单次请求上限（网站 maxlimit=30000，422 个商品一页拿完）
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/honeymew_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("honeymew-monitor")

# ======================== 网络请求 ========================
def fetch_url(url):
    """抓取 URL 返回文本"""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "application/json,*/*",
        "Referer": BASE_URL + "/category_all"
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

# ======================== 商品获取 ========================
def fetch_all_products():
    """
    用 productclass2 JSON 接口一次性获取全部商品
    每条记录 = 一个商品的一个颜色（sku 字段内是全部尺码）
    422 条记录只需 1 个请求
    """
    all_products = []
    start = 0
    while True:
        url = (f"{PRODUCTS_API}?limit={PAGE_LIMIT}&start={start}"
               "&part.status_id=1&orderby=max_start_at~desc"
               "&with=aux,belonging_to,indexsummary"
               f"&tagp={quote(TAGP)}")
        data = fetch_json(url)
        prods = data.get("data", [])
        if not prods:
            break
        all_products.extend(prods)
        total = data.get("total", len(all_products))
        logger.info(f"start={start}: {len(prods)} 条记录 (累计 {len(all_products)}/{total})")
        if len(all_products) >= total or len(prods) < PAGE_LIMIT:
            break
        start += PAGE_LIMIT
    return all_products

def parse_record(p):
    """
    解析一条颜色记录
    返回: {name, number, color, url, main_image, sku_images, skus}
    skus: {"颜色 / 尺码": 是否卖空}
    """
    color = p.get("color_japanese", "") or ""
    # 解析 sku 字段（JSON 字符串，每项一个尺码）
    try:
        sku_list = json.loads(p.get("sku", "[]"))
    except Exception:
        sku_list = []

    skus = {}
    sku_images = {}
    for s in sku_list:
        # 尺码名从 tags 里取（如 XSサイズ / フリーサイズ）
        size = ""
        for t in (s.get("tags") or []):
            size = t.get("label", "") or t.get("name", "")
            break
        sku_name = f"{color} / {size}" if (color and size) else (color or size or s.get("ean", "?"))
        stock = s.get("web_stock_quantity", 0)
        if stock is None:
            stock = 0
        skus[sku_name] = (stock <= 0)
        img = s.get("image_url", "")
        if img and sku_name not in sku_images:
            sku_images[sku_name] = img

    # 详情页 URL（用 root 商品的 shortid）
    url = ""
    bt = p.get("belonging_to") or []
    if bt:
        url = f"{BASE_URL}/item/{bt[0].get('shortid', '')}"
    elif p.get("shortid"):
        url = f"{BASE_URL}/item/{p['shortid']}"

    # 主图
    main_image = ""
    images = p.get("images") or []
    if images:
        main_image = images[0].get("url", "") or ""

    return {
        "name": p.get("product_name", "") or p.get("root_name", "") or "?",
        "number": p.get("root_product_number", ""),
        "color": color,
        "url": url,
        "main_image": main_image,
        "sku_images": sku_images,
        "skus": skus,
    }
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
        subject = f"🚨 [honeymew] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[honeymew]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [honeymew] {len(events)} 个SKU卖空"
        title = f"[honeymew]{len(events)}个SKU卖空"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [honeymew] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [honeymew] 卖空告警\n\n"
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
        subject = f"📦 [honeymew] 补货: {e['product_name']} - {e['sku']}"
        title = f"[honeymew]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [honeymew] {len(events)} 个SKU补货"
        title = f"[honeymew]{len(events)}个SKU补货"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#27ae60;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#27ae60;">📦 [honeymew] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [honeymew] 补货通知\n\n"
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
        subject = f"🆕 [honeymew] 上新: {e['product_name']} ({e['sku']})"
        title = f"[honeymew]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [honeymew] {len(events)} 个上新"
        title = f"[honeymew]{len(events)}个上新"
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:#e67e22;font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [honeymew] 上新通知</h2><p>{len(events)} 个新商品上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = "### [honeymew] 上新通知\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- 颜色: {e['sku']}\n- [查看商品]({e['url']})\n"
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
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "シェルピンク / XSサイズ", "number": "NJJ0067C", "url": "https://mycolor.jp/honeymew/item/test", "image": "", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "ブラック / フリーサイズ", "number": "NJJ0067C", "url": "https://mycolor.jp/honeymew/item/test", "image": "", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试-上新】", "sku": "シェルピンク", "number": "NJJ0067C", "url": "https://mycolor.jp/honeymew/item/test", "image": "", "time": datetime.now().isoformat()},
        ])
        return

    # 一次拿全部商品（含全部 SKU 库存状态）
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
    seen_ids = set(prev.get("_seen_ids", []))
    # 双保险：如果 _seen_ids 为空，但 prev 里有旧商品，用旧商品 ID 初始化
    if not seen_ids:
        seen_ids = {k for k in prev.keys() if not k.startswith("_")}
        if seen_ids:
            logger.info(f"从旧状态恢复 {len(seen_ids)} 个已见商品")

    # 首次运行判断：没有任何商品的 SKU 状态 → 建基线，不发通知
    products_with_skus = len([k for k in prev.keys() if not k.startswith("_")])
    first_run = products_with_skus == 0
    if first_run:
        logger.info("首次运行：建立基线状态，本轮不发通知")
    else:
        logger.info(f"honeymew 监控启动: 本轮全量扫描 {len(products)} 条颜色记录 (已有状态 {products_with_skus} 条)")

    new_state = {}
    events = []

    for p in products:
        rid = p.get("shortid", "")
        if not rid:
            continue
        info = parse_record(p)
        if not info["skus"]:
            continue
        new_state[rid] = {"name": info["name"], "skus": info["skus"]}

        is_new = rid not in seen_ids
        if is_new:
            seen_ids.add(rid)

        # 首轮建基线：只记录状态，不做事件检测
        if first_run:
            continue

        if is_new:
            # ===== 上新（颜色级，SKU 字段显示颜色）=====
            first_sku = next(iter(info["skus"].keys()), "")
            sku_image = info["sku_images"].get(first_sku, "") or info["main_image"]
            events.append({"type": "NEW", "product_name": info["name"], "sku": info["color"] or first_sku, "number": info["number"], "url": info["url"], "image": sku_image, "time": datetime.now().isoformat()})
            logger.info(f"🆕 上新: {info['name']} ({info['color']})")
        else:
            # ===== 卖空/补货对比（颜色×尺码级）=====
            prev_skus = prev.get(rid, {}).get("skus", {})
            for sn, so in info["skus"].items():
                was = prev_skus.get(sn, False)
                sku_image = info["sku_images"].get(sn, "") or info["main_image"]
                if so and not was:
                    events.append({"type": "SOLD_OUT", "product_name": info["name"], "sku": sn, "number": info["number"], "url": info["url"], "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {info['name']} - {sn}")
                elif not so and was:
                    events.append({"type": "RESTOCK", "product_name": info["name"], "sku": sn, "number": info["number"], "url": info["url"], "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {info['name']} - {sn}")

    logger.info(f"本轮扫描完成: {len(new_state)} 条颜色记录, {len(events)} 个变化")

    if first_run:
        logger.info(f"基线建立完成: {len(new_state)} 条颜色记录已记录，下一轮开始正常监控")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")

    # 合并旧状态（保留异常记录和已下架商品的旧数据，防止误报）
    final_state = dict(prev)
    final_state.update(new_state)
    final_state["_seen_ids"] = sorted(seen_ids)
    save_state(final_state)
    logger.info(f"状态已保存（已记录 {len(seen_ids)} 条）")

if __name__ == "__main__":
    main()
