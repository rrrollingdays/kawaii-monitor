#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
piumofficial.com 卖空/补货监控脚本
- Shopify 平台，通过 .json 接口获取商品数据
- 通过 variant 的 available 字段判断是否卖空
- 支持卖空通知 + 补货通知
- 邮件 + 微信双通道通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, unquote

# ======================== 配置 ========================
BASE_URL = "https://piumofficial.com"
COLLECTION_URL = "https://piumofficial.com/collections/all-items"
MAX_PAGES = 20          # 最多扫描的页数
MAX_WORKERS = 2         # 并发抓取数
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
        "Accept": "text/html,application/json,*/*"
    })
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    # Shopify 返回 UTF-8
    return raw.decode("utf-8", errors="replace")

def fetch_url_with_retry(url, retries=4, base_delay=5):
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

# ======================== 商品发现 ========================
def discover_product_handles():
    """从 all-items 集合页发现所有商品 handle"""
    handles = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{COLLECTION_URL}?page={page}"
        try:
            html = fetch_url_with_retry(url)
            # 匹配 /products/{handle} 链接，去掉 ?variant= 后缀
            found = re.findall(r'href="/products/([^"?"]+)', html)
            if not found:
                logger.info(f"第 {page} 页无商品，停止翻页")
                break
            before = len(handles)
            handles.update(found)
            added = len(handles) - before
            logger.info(f"第 {page} 页: 发现 {added} 个新商品 (累计 {len(handles)})")
            # 如果本页没有新增，说明到最后一页了
            if added == 0 and page > 1:
                break
        except Exception as e:
            logger.warning(f"抓取列表页失败: page={page} → {e}")
            if page == 1:
                # 首页失败，本轮数据不可信，返回空列表
                logger.warning("首页抓取失败，本轮不覆盖状态文件")
                return []
            break
    logger.info(f"共发现 {len(handles)} 个商品")
    return sorted(handles)

# ======================== 商品解析 ========================
def parse_product(handle):
    """
    通过 Shopify .json 接口获取商品信息
    返回: {name, handle, url, skus: [{name, sold_out}]}
    """
    json_url = f"{BASE_URL}/products/{handle}.json"
    try:
        data = fetch_json(json_url)
    except Exception as e:
        logger.warning(f"JSON抓取失败: {handle} → {e}")
        # 退回到 HTML 解析
        return parse_product_html(handle)

    product = data.get("product", {})
    if not product:
        return parse_product_html(handle)

    name = product.get("title", handle)
    variants = product.get("variants", [])

    # 如果 JSON 没有 available 字段，需要从 HTML 获取
    skus = []
    need_html = True
    for v in variants:
        vtitle = v.get("title", "")
        available = v.get("available", None)
        if available is not None:
            need_html = False
        skus.append({
            "name": vtitle,
            "variant_id": v.get("id", ""),
            "sold_out": not available if available is not None else None
        })

    if need_html:
        # JSON 不含 available，从 HTML form 解析
        return parse_product_html(handle, name, skus)

    # 提取商品主图
    image = ""
    images = product.get("images", [])
    if images:
        image = images[0].get("src", "")
        if image:
            if image.startswith("//"):
                image = "https:" + image
            elif image.startswith("http://"):
                image = "https://" + image[7:]
            image = image.replace("&", "&").split("&width")[0].split("?width")[0]

    # 提取每个 variant 对应的图片
    # Shopify images 里有 variant_ids 字段关联 variant
    # 如果没有 variant_ids，用 alt 字段里的 "カラー：xxx" 匹配
    variant_images = {}
    for img in images:
        src = img.get("src", "")
        if not src:
            continue
        # 修正 URL：确保 https，清理 HTML 实体
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("http://"):
            src = "https://" + src[7:]
        src = src.replace("&", "&").split("&width")[0].split("?width")[0]
        vids = img.get("variant_ids", [])
        alt = img.get("alt", "") or ""
        # 优先用 variant_ids 关联
        for vid in vids:
            if vid and src and vid not in variant_images:
                variant_images[vid] = src
        # 如果没有 variant_ids，用 alt 里的颜色名匹配
        if not vids and alt:
            alt_match = re.search(r'カラー[：:\s]*(.+)', alt)
            if alt_match:
                color = alt_match.group(1).strip()
                for s in skus:
                    sname = s.get("name", "")
                    # variant name 通常是 "ピンク / Free" 格式，取第一部分
                    color_part = sname.split("/")[0].strip() if "/" in sname else sname
                    if color_part == color and s["variant_id"] not in variant_images:
                        variant_images[s["variant_id"]] = src

    return {
        "name": name,
        "handle": handle,
        "url": f"{BASE_URL}/products/{handle}",
        "image": image,
        "variant_images": variant_images,
        "skus": skus
    }

def parse_product_html(handle, name=None, json_skus=None):
    """
    从商品详情页 HTML 解析 SKU 卖空状态
    Shopify 的 wl-variant-list 中，每个 variant 有一个 form:
    - 有 BIS_trigger / Sold Out = 卖空
    - 有 カートに入れる = 有货
    """
    url = f"{BASE_URL}/products/{handle}"
    try:
        html = fetch_url_with_retry(url)
    except Exception as e:
        logger.warning(f"HTML抓取失败: {handle} → {e}")
        return None

    if not name:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        name = m.group(1).strip() if m else handle

    # 获取 variant title → variant_id 映射（来自 JSON 或 HTML input）
    vid_map = {}
    if json_skus:
        for s in json_skus:
            vid_map[s["variant_id"]] = s["name"]
    else:
        for m in re.finditer(r'data-variant-id="(\d+)"[^>]*data-option-value="([^"]*)"', html):
            vid_map[m.group(1)] = m.group(2)
        # 也尝试反向匹配
        if not vid_map:
            for m in re.finditer(r'data-option-value="([^"]*)"[^>]*data-variant-id="(\d+)"', html):
                vid_map[m.group(2)] = m.group(1)

    # 解析每个 variant form 的卖空状态
    # 每个包含 value="{vid}" 的 form 代表一个 variant
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
        # 如果没找到 form，尝试从 color-swatch 的 data-variant-inventory 判断
        for m in re.finditer(r'data-variant-id="(\d+)"[^>]*?data-option-value="([^"]*)"', html, re.DOTALL):
            vid, title = m.group(1), m.group(2)
            # 检查附近是否有 sold-out class
            ctx_start = m.start()
            ctx_end = min(len(html), m.end() + 500)
            ctx = html[ctx_start:ctx_end]
            sold_out = "sold-out" in ctx.lower() or "Sold Out" in ctx
            skus.append({"name": title, "variant_id": vid, "sold_out": sold_out})

    # 如果还是没找到，使用 JSON 的 variant 列表但标记为未知
    if not skus and json_skus:
        skus = json_skus

    # 提取商品主图（og:image 或第一张图片）
    image = ""
    og_m = re.search(r'<meta[^>]*property=\"og:image\"[^>]*content=\"([^\"]+)\"', html)
    if og_m:
        image = og_m.group(1)
    else:
        img_m = re.search(r'<img[^>]*class=\"[^\"]*product-image[^\"]*\"[^>]*src=\"([^\"]+)\"', html)
        if img_m:
            image = img_m.group(1)
    if image:
        if image.startswith("//"):
            image = "https:" + image
        elif image.startswith("http://"):
            image = "https://" + image[7:]
        image = image.replace("&", "&").split("&width")[0].split("?width")[0]

    # 提取每个 variant 对应的图片
    variant_images = {}
    # 从 HTML 中找 data-variant-id 和对应的图片
    for m in re.finditer(r'data-variant-id="(\d+)"[^>]*?(?:data-image[^>]*?src="([^"]+)"|src="([^"]+)"[^>]*?data-variant-id)', html, re.DOTALL):
        vid = m.group(1)
        src = m.group(2) or m.group(3) or ""
        if src:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("http://"):
                src = "https://" + src[7:]
            src = src.replace("&", "&").split("&width")[0].split("?width")[0]
        if vid and src and vid not in variant_images:
            variant_images[vid] = src
    # 也尝试从 alt 属性匹配
    for m in re.finditer(r'<img[^>]*src="([^"]+)"[^>]*alt="カラー[：:\s]*([^"]+)"', html):
        src = m.group(1)
        if src:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("http://"):
                src = "https://" + src[7:]
            src = src.replace("&", "&").split("&width")[0].split("?width")[0]
        color = m.group(2).strip()
        for s in skus:
            sname = s.get("name", "")
            color_part = sname.split("/")[0].strip() if "/" in sname else sname
            if color_part == color and s.get("variant_id") and s["variant_id"] not in variant_images:
                variant_images[s["variant_id"]] = src

    return {
        "name": name,
        "handle": handle,
        "url": url,
        "image": image,
        "variant_images": variant_images,
        "skus": skus
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
def check_product(handle):
    """抓取单个商品的 SKU 状态"""
    try:
        return handle, parse_product(handle)
    except Exception as e:
        logger.warning(f"抓取失败: {handle} → {e}")
        return handle, None

def main():
    if "--test-notify" in sys.argv:
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】", "sku": "グレー / Free", "number": "1026a070301181", "url": "https://piumofficial.com/products/test", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】", "sku": "ブラック / Free", "number": "1026a070301181", "url": "https://piumofficial.com/products/test", "time": datetime.now().isoformat()},
        ])
        return

    handles = discover_product_handles()
    if not handles:
        logger.warning("未获取到商品列表，本轮跳过，保留旧状态")
        return
    logger.info(f"pium 监控启动: {len(handles)} 个商品")

    prev = load_state()
    new_state = {}
    events = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_product, h): h for h in handles}
        for f in as_completed(futures):
            handle, info = f.result()
            if not info:
                # 抓取失败，保留旧状态
                url = f"{BASE_URL}/products/{handle}"
                if url in prev:
                    new_state[url] = prev[url]
                    logger.info(f"抓取失败，保留旧状态: {handle}")
                continue
            url = info["url"]
            # SKU 状态: {sku_name: sold_out}
            cur = {}
            for s in info["skus"]:
                if s.get("sold_out") is not None:
                    cur[s["name"]] = s["sold_out"]
            if not cur:
                continue
            new_state[url] = {"name": info["name"], "skus": cur}
            variant_images = info.get("variant_images", {})
            main_image = info.get("image", "")
            # 货号 = handle（URL解码 + 去 -copy 等后缀）
            raw_handle = unquote(info.get("handle", ""))
            product_number = re.sub(r'-copy$|-1$|-2$', '', raw_handle)
            prev_skus = prev.get(url, {}).get("skus", {})
            for sn, so in cur.items():
                was = prev_skus.get(sn, False)
                # 找到这个 SKU 名称对应的 variant_id
                sku_image = main_image
                for s in info["skus"]:
                    if s["name"] == sn and s.get("variant_id"):
                        vid = s["variant_id"]
                        if vid in variant_images:
                            sku_image = variant_images[vid]
                        break
                if so and not was:
                    events.append({"type": "SOLD_OUT", "product_name": info["name"], "sku": sn, "number": product_number, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {info['name']} - {sn}")
                elif not so and was:
                    events.append({"type": "RESTOCK", "product_name": info["name"], "sku": sn, "number": product_number, "url": url, "image": sku_image, "time": datetime.now().isoformat()})
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
