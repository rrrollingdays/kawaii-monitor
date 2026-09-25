#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
palcloset.jp / OLIVE des OLIVE 卖空/补货/上新监控脚本 v2
- v1 用列表页 SKU 集合做"出现/消失"检测 → 误报严重：
  Palcloset 列表页不反映库存状态（全卖空商品仍挂在列表），
  且列表有 CDN 显示抖动（同一 SKU 时隐时现）→ 卖空/补货交替误报
- v2 改为详情页真相源：
  * 列表页（4页）只用于发现商品 ID 集合 + 新商品
  * 每个商品的详情页是服务端渲染的稳定数据：颜色×尺码×在庫あり/在庫なし
  * 每轮全量体检（约 153 商品 = 157 请求），状态翻转才发通知
- 防抖动三重保险：
  * 商品详情页连续 3 次抓取失败才认定下架（中间轮沿用旧状态）
  * 一轮内详情页失败率 >30% → 本轮作废（数据不可信）
  * 通知只在 OK↔OUT 真实翻转时发，列表抖动不再产生事件
- 邮件(多收件人) + 微信 + Bark(带图) 三通道通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

# ======================== 配置 ========================
BASE_URL = "https://www.palcloset.jp"
BRAND = "olivedesolive"
LIST_URL = f"{BASE_URL}/display/display/?mode=zSearch&SearchItem.SORT_KEY=RELEASE_DM&b={BRAND}"
MAX_PAGES = 8            # 列表页最多翻 8 页（当前 4 页够用）
REQUEST_TIMEOUT = 25
LIST_PAGE_DELAY = 2      # 列表页翻页间隔
ITEM_DELAY = 0.4         # 详情页请求间隔（对服务器友好）
MAX_ITEM_FAILURE_RATE = 0.30   # 一轮内详情页失败率超 30% → 本轮作废
ITEM_FAIL_CONFIRM = 3    # 商品连续 3 次抓取失败才认定下架移除

SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
BARK_URL = os.environ.get("BARK_URL", "").rstrip("/")
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/olive_monitor_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("olive-monitor")

# ======================== 网络请求 ========================
def fetch_url(url):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,*/*"
    })
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")

def fetch_url_with_retry(url, retries=3, base_delay=2):
    for attempt in range(1, retries + 1):
        try:
            return fetch_url(url)
        except HTTPError as e:
            if e.code in (429, 503) and attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"{e.code} 限流，{delay}s 后重试 ({attempt}/{retries})")
                time.sleep(delay)
                continue
            raise
        except Exception:
            if attempt < retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise

# ======================== 列表页：发现商品 ========================
def parse_list_page(html):
    """解析一页列表 HTML，返回商品列表（goods_id + 名称 + 图）"""
    goods = {}
    for m in re.finditer(r'href="/display/item/([^/?"]+)/\?cl=(\d+)', html):
        gid = m.group(1)
        end = html.find("</a>", m.start())
        block = html[m.start():end if end > 0 else m.start() + 6000]
        dl = re.search(r"&#39;items&#39;: \[(\{.*?\})\]", block)
        if not dl:
            continue
        raw = dl.group(1).replace("&#39;", "'")
        pairs = dict(re.findall(r"'(\w+)':'([^']*)'", raw))
        name = pairs.get("item_name", "")
        if not name:
            continue
        img = ""
        im = re.search(r'data-src="([^"]+)"', block)
        if im:
            img = im.group(1).replace("&amp;", "&")
        goods.setdefault(gid, {"name": name, "image": img, "url": f"{BASE_URL}/display/item/{gid}/?b={BRAND}"})
    return list(goods.values())

def fetch_list_goods():
    """翻页拉列表页，返回当轮商品集合 {gid: info}。任何一页失败整轮作废"""
    result = {}
    for page in range(1, MAX_PAGES + 1):
        html = fetch_url_with_retry(f"{LIST_URL}&p={page}")
        page_goods = parse_list_page(html)
        logger.info(f"列表第 {page} 页: {len(page_goods)} 个商品 (累计 {len(result) + len(page_goods)})")
        if not page_goods:
            break
        for g in page_goods:
            result[g["url"]] = g
        if len(page_goods) < 10:
            break
        time.sleep(LIST_PAGE_DELAY)
    # 按 gid 重组
    out = {}
    for g in result.values():
        gid = g["url"].split("/item/")[1].split("/")[0]
        out[gid] = g
    return out

# ======================== 详情页：库存真相 ========================
def parse_item_page(html):
    """
    解析商品详情页，返回 {sku_cd: {"variant":颜色, "size":尺码, "state":"OK"/"OUT"}}
    结构：每个颜色一个 cart_pic__desc__color 标记，其后每个尺码一个 <dl id="skuEvent">
    dt 文本形态："FREE/在庫あり"、"M/在庫なし"、可附 deliveryplan（预约出荷）
    """
    colors = [(m.start(), m.group(1)) for m in re.finditer(r'cart_pic__desc__color">カラー：([^<]+)</p>', html)]
    dls = [(m.start(), m.group(0)) for m in re.finditer(r'<dl class="clearfix f_wrap"[^>]*id="skuEvent"[^>]*>.*?</dl>', html, re.S)]
    if not dls:
        return {}
    skus = {}
    cur_color = ""
    ci = 0
    for pos, dl in dls:
        while ci < len(colors) and colors[ci][0] < pos:
            cur_color = colors[ci][1]
            ci += 1
        dt_m = re.search(r'<dt>(.*?)</dt>', dl, re.S)
        if not dt_m:
            continue
        dt_text = re.sub(r"\s+", " ", dt_m.group(1)).strip()
        sku_m = re.search(r'name="shopSkuCd" value="(\d+)"', dl)
        if not sku_m:
            # 无有效 SKU 编号 = JS 模板块（display:none 的克隆源），跳过
            continue
        state = "OUT" if "在庫なし" in dt_text else "OK"
        size = dt_text.split("/")[0].strip() if "/" in dt_text else ""
        # 预约（予約）状态也算 OK（可以下单）；仅"在庫なし"才算卖空
        skus[sku_m.group(1)] = {"variant": cur_color, "size": size, "state": state}
    return skus

def fetch_item_state(gid):
    """抓单商品详情页并解析。返回 (skus, ok)"""
    try:
        html = fetch_url_with_retry(f"{BASE_URL}/display/item/{gid}/")
    except HTTPError as e:
        logger.warning(f"详情页 HTTP {e.code}: {gid}")
        return None, False
    except Exception as e:
        logger.warning(f"详情页抓取失败: {gid} {e}")
        return None, False
    skus = parse_item_page(html)
    if not skus:
        logger.warning(f"详情页解析为空（结构变化或被拦截）: {gid}")
        return None, False
    return skus, True

# ======================== 通知 ========================
def send_email(subject, body_html):
    recipients = [x.strip() for x in EMAIL_TO.split(",") if x.strip()]
    if not SMTP_USER or not SMTP_PASSWORD or not recipients:
        logger.warning("邮件配置不完整，跳过")
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
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
                    s.sendmail(SMTP_USER, recipients, msg.as_string())
            else:
                with smtplib.SMTP_SSL(SMTP_SERVER, 465, context=ctx, timeout=15) as s:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                    s.sendmail(SMTP_USER, recipients, msg.as_string())
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

def send_bark(title, desp):
    if not BARK_URL:
        logger.warning("Bark 未配置，跳过")
        return False
    m = re.search(r'src="([^"]+)"', desp)
    image_url = m.group(1) if m else ""
    text = re.sub(r"<[^>]+>", "", desp)
    text = text.replace("**", "").replace("### ", "")[:900]
    payload = {"title": title[:60], "body": text, "group": "kawaii-monitor", "level": "timeSensitive"}
    if image_url:
        payload["image"] = image_url
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

def _event_rows(events, color):
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    return rows

def _event_desp(events):
    desp = ""
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    return desp

def _notify_soldout(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 [olive] 卖空: {e['product_name']} - {e['sku']}"
        title = f"[olive]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [olive] {len(events)} 个SKU卖空"
        title = f"[olive]{len(events)}个SKU卖空"
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [OLIVE des OLIVE] 商品卖空</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{_event_rows(events, "#e74c3c")}</table></body></html>'
    desp = "### [olive] 卖空\n\n" + _event_desp(events)
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_restock(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📦 [olive] 补货: {e['product_name']} - {e['sku']}"
        title = f"[olive]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [olive] {len(events)} 个SKU补货"
        title = f"[olive]{len(events)}个SKU补货"
    body = f'<html><body><h2 style="color:#27ae60;">📦 [OLIVE des OLIVE] 补货通知</h2><p>{len(events)} 个SKU补货:</p><table style="border-collapse:collapse;">{_event_rows(events, "#27ae60")}</table></body></html>'
    desp = "### [olive] 补货通知\n\n" + _event_desp(events)
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def _notify_new(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🆕 [olive] 上新: {e['product_name']} - {e['sku']}"
        title = f"[olive]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [olive] {len(events)} 个SKU上新"
        title = f"[olive]{len(events)}个上新"
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [OLIVE des OLIVE] 上新通知</h2><p>{len(events)} 个SKU上新:</p><table style="border-collapse:collapse;">{_event_rows(events, "#e67e22")}</table></body></html>'
    desp = "### [olive] 上新通知\n\n" + _event_desp(events)
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def notify_events(events):
    if not events:
        return
    soldout = [e for e in events if e["type"] == "SOLD_OUT"]
    restock = [e for e in events if e["type"] == "RESTOCK"]
    new = [e for e in events if e["type"] == "NEW"]
    if new:
        _notify_new(new)
    if soldout:
        _notify_soldout(soldout)
    if restock:
        _notify_restock(restock)

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
            {"type": "RESTOCK", "product_name": "【测试】Aimerianejoieキルティングキャリーオントート", "sku": "ミント / FREE", "number": "1051200120", "url": "https://www.palcloset.jp/display/item/1051200120/", "image": "https://contents.palcloset.jp/static/images/item/652890_3064210_1.jpg", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试】ナポレオンジップブルゾン", "sku": "サックスブルー / FREE", "number": "1062060200", "url": "https://www.palcloset.jp/display/item/1062060200/", "image": "https://contents.palcloset.jp/static/images/item/746200_3068991_1.jpg", "time": datetime.now().isoformat()},
        ])
        return

    # ===== 第 1 步：列表页发现商品集合 =====
    try:
        list_goods = fetch_list_goods()
    except Exception as e:
        logger.warning(f"列表页抓取失败: {e}，本轮作废，保留旧状态")
        return
    if len(list_goods) < 10:
        logger.warning(f"列表仅 {len(list_goods)} 商品，数据异常，本轮作废")
        return

    prev = load_state()
    prev_goods = prev.get("_goods", {})           # {gid: {"name","image","url","skus":{}}}
    seen_goods = set(prev.get("_seen_goods", list(prev_goods.keys())))
    fail_count = prev.get("_fail_count", {})

    # 目标集合 = 当轮列表 ∪ 历史见过（新商品自动纳入；列表抖动不再影响覆盖）
    targets = sorted(set(list_goods.keys()) | seen_goods)
    first_run = len(prev_goods) == 0
    logger.info(f"{'首次运行' if first_run else 'olive v2 启动'}: 本轮体检 {len(targets)} 个商品 (列表发现 {len(list_goods)})")

    events = []
    new_goods_state = {}
    failed = 0

    for gid in targets:
        info = list_goods.get(gid) or prev_goods.get(gid, {})
        skus, ok = fetch_item_state(gid)
        if not ok:
            # 失败处理：连续失败达阈值 → 静默下架；否则沿用旧状态
            fail_count[gid] = fail_count.get(gid, 0) + 1
            failed += 1
            if fail_count[gid] >= ITEM_FAIL_CONFIRM:
                logger.warning(f"商品连续 {fail_count[gid]} 次抓取失败，从监控移除: {gid}")
                fail_count.pop(gid, None)
                seen_goods.discard(gid)
            else:
                if gid in prev_goods:
                    new_goods_state[gid] = prev_goods[gid]  # 沿用旧状态，不产生事件
            time.sleep(ITEM_DELAY)
            continue

        fail_count.pop(gid, None)
        name = info.get("name", "")
        image = info.get("image", "")
        url = info.get("url", f"{BASE_URL}/display/item/{gid}/")

        if first_run:
            new_goods_state[gid] = {"name": name, "image": image, "url": url, "skus": skus}
        elif gid not in prev_goods:
            # ===== 新商品 =====
            first_sku = next(iter(skus.values()), {})
            sku_label = f"{first_sku.get('variant','')} / {first_sku.get('size','')}".strip(" /")
            events.append({"type": "NEW", "product_name": name, "sku": sku_label,
                           "number": gid, "url": url, "image": image,
                           "time": datetime.now().isoformat()})
            logger.info(f"🆕 上新: {name} ({len(skus)} SKU)")
            new_goods_state[gid] = {"name": name, "image": image, "url": url, "skus": skus}
        else:
            # ===== 老商品：SKU 状态对比 =====
            prev_skus = prev_goods.get(gid, {}).get("skus", {})
            for scd, s in skus.items():
                label = f"{s['variant']} / {s['size']}".strip(" /")
                old = prev_skus.get(scd)
                if old is None:
                    events.append({"type": "NEW", "product_name": name, "sku": label,
                                   "number": gid, "url": url, "image": image,
                                   "time": datetime.now().isoformat()})
                    logger.info(f"🆕 新SKU: {name} - {label}")
                elif old.get("state") == "OUT" and s["state"] == "OK":
                    events.append({"type": "RESTOCK", "product_name": name, "sku": label,
                                   "number": gid, "url": url, "image": image,
                                   "time": datetime.now().isoformat()})
                    logger.info(f"📦 补货: {name} - {label}")
                elif old.get("state") == "OK" and s["state"] == "OUT":
                    events.append({"type": "SOLD_OUT", "product_name": name, "sku": label,
                                   "number": gid, "url": url, "image": image,
                                   "time": datetime.now().isoformat()})
                    logger.info(f"🚨 卖空: {name} - {label}")
            new_goods_state[gid] = {"name": name, "image": image, "url": url, "skus": skus}

        time.sleep(ITEM_DELAY)

    # 失败率防线：详情页大面积失败 → 数据不可信 → 本轮作废
    if targets and failed / len(targets) > MAX_ITEM_FAILURE_RATE:
        logger.warning(f"详情页失败率 {failed}/{len(targets)} 超阈值，本轮作废，保留旧状态")
        return

    logger.info(f"本轮完成: {len(targets)} 商品体检, {failed} 失败, {len(events)} 个变化")

    if first_run:
        logger.info(f"基线建立完成: {len(new_goods_state)} 商品已记录，下一轮开始正常监控")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")

    seen_goods |= set(new_goods_state.keys())
    save_state({
        "_goods": new_goods_state,
        "_seen_goods": sorted(seen_goods),
        "_fail_count": fail_count,
    })
    logger.info(f"状态已保存（{len(seen_goods)} 个商品）")

if __name__ == "__main__":
    main()
