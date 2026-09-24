#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
palcloset.jp / OLIVE des OLIVE 卖空/补货/上新监控脚本
- Palcloset 平台（非 Shopify），通过品牌列表页 HTML 解析
- 列表页每个格子 = 一个 SKU（商品×颜色），SKU 卖空后从列表消失
- 监控逻辑：SKU 出现（上新/新色/补货回归）+ SKU 消失（卖空）
- 每轮拉 4 页列表（约 385 个 SKU），单页失败整轮作废防误报
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
MAX_PAGES = 8          # 最多翻 8 页（当前 4 页，留余量自动扩容）
REQUEST_TIMEOUT = 25
PAGE_DELAY = 2         # 翻页间隔，对服务器友好
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
SNAPSHOT_MIN_RATIO = 0.6   # 本轮 SKU 数低于上轮 60% 时，跳过卖空检测（防半页失败误报）

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
    """抓取 URL 返回文本"""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,*/*"
    })
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")

def fetch_url_with_retry(url, retries=3, base_delay=2):
    """带退避重试的抓取"""
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

# ======================== 商品获取 ========================
def parse_list_page(html):
    """
    解析一页列表 HTML，返回 SKU 列表
    每个格子 = <a href="/display/item/{goods_id}/?cl={cl}" onclick="dataLayer.push({...items:[{...}]})">
    dataLayer 内含结构化数据: item_id(SKU全号)/item_name/price/item_variant
    """
    skus = []
    # 逐个 <a href="/display/item/... 切窗口到 </a>
    for m in re.finditer(r'<a href="/display/item/([^/?"]+)/\?cl=(\d+)', html):
        goods_id, cl = m.group(1), m.group(2)
        end = html.find("</a>", m.start())
        block = html[m.start():end if end > 0 else m.start() + 6000]
        # dataLayer 结构化数据
        dl = re.search(r"&#39;items&#39;: \[(\{.*?\})\]", block)
        if not dl:
            continue
        raw = dl.group(1).replace("&#39;", "'")
        pairs = dict(re.findall(r"'(\w+)':'([^']*)'", raw))
        sku_id = pairs.get("item_id", "")
        if not sku_id:
            continue
        # 图片（懒加载 data-src）
        img = ""
        im = re.search(r'data-src="([^"]+)"', block)
        if im:
            img = im.group(1).replace("&amp;", "&")
        skus.append({
            "sku_id": sku_id,
            "goods_id": goods_id,
            "cl": cl,
            "name": pairs.get("item_name", ""),
            "variant": pairs.get("item_variant", ""),
            "price": pairs.get("price", ""),
            "category": pairs.get("item_category", ""),
            "image": img,
            "url": f"{BASE_URL}/display/item/{goods_id}/?cl={cl}&b={BRAND}",
        })
    # 同页可能重复（PC/SP 双版本），按 sku_id 去重
    seen = {}
    for s in skus:
        seen.setdefault(s["sku_id"], s)
    return list(seen.values())

def fetch_all_skus():
    """
    翻页拉取品牌全部在售 SKU
    任何一页失败 → 整轮作废（抛异常），防止半页数据导致大量"假卖空"
    """
    all_skus = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"{LIST_URL}&p={page}"
        html = fetch_url_with_retry(url)  # 失败直接抛
        page_skus = parse_list_page(html)
        logger.info(f"第 {page} 页: {len(page_skus)} 个SKU (累计 {len(all_skus) + len(page_skus)})")
        if not page_skus:
            break
        for s in page_skus:
            all_skus[s["sku_id"]] = s
        if len(page_skus) < 20:   # 最后一页（当前尾页 25 格，阈值放 20）
            break
        time.sleep(PAGE_DELAY)
    return list(all_skus.values())

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

def _event_rows(events, color):
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}<br><span style="color:#999;font-size:12px;">{e.get("number", "")}</span></td><td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold;">{e["sku"]}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    return rows

def _event_desp(events):
    desp = ""
    for e in events:
        desp += f"**{e['product_name']}**\n- 货号: {e.get('number', '无')}\n- SKU: {e['sku']}\n- [查看商品]({e['url']})\n"
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
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [OLIVE des OLIVE] 商品卖空告警</h2><p>{len(events)} 个SKU卖空:</p><table style="border-collapse:collapse;">{_event_rows(events, "#e74c3c")}</table></body></html>'
    desp = "### [olive] 卖空告警\n\n" + _event_desp(events)
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
    body = f'<html><body><h2 style="color:#27ae60;">📦 [OLIVE des OLIVE] 补货通知</h2><p>{len(events)} 个SKU已补货上架:</p><table style="border-collapse:collapse;">{_event_rows(events, "#27ae60")}</table></body></html>'
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
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [OLIVE des OLIVE] 上新通知</h2><p>{len(events)} 个SKU上架:</p><table style="border-collapse:collapse;">{_event_rows(events, "#e67e22")}</table></body></html>'
    desp = "### [olive] 上新通知\n\n" + _event_desp(events)
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

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
            {"type": "RESTOCK", "product_name": "【测试】Aimerianejoieキルティングキャリーオントート", "sku": "ミント", "number": "1051200120", "url": "https://www.palcloset.jp/display/item/1051200120/?cl=19&b=olivedesolive", "image": "https://contents.palcloset.jp/static/images/item/652890_3064210_1.jpg", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试】ナポレオンジップブルゾン", "sku": "サックスブルー", "number": "1062060200", "url": "https://www.palcloset.jp/display/item/1062060200/?cl=34&b=olivedesolive", "image": "https://contents.palcloset.jp/static/images/item/746200_3068991_1.jpg", "time": datetime.now().isoformat()},
        ])
        return

    # 拉取品牌全部在售 SKU（任何一页失败整轮作废）
    try:
        skus = fetch_all_skus()
    except Exception as e:
        logger.warning(f"获取列表失败: {e}，本轮跳过，保留旧状态")
        return
    if len(skus) < 10:
        logger.warning(f"仅抓到 {len(skus)} 个SKU，数据异常，本轮作废")
        return

    prev = load_state()
    prev_snapshot = prev.get("_snapshot", {})      # 上一轮 SKU 快照
    seen_skus = prev.get("_seen_skus", {})          # 历史见过的全部 SKU（永不删除）

    first_run = len(prev_snapshot) == 0
    if first_run:
        logger.info("首次运行：建立基线状态，本轮不发通知")
    else:
        logger.info(f"olive 监控启动: 本轮 {len(skus)} 个SKU (上轮 {len(prev_snapshot)})")

    cur_snapshot = {}
    for s in skus:
        cur_snapshot[s["sku_id"]] = {
            "name": s["name"], "variant": s["variant"], "price": s["price"],
            "goods_id": s["goods_id"], "cl": s["cl"], "image": s["image"],
            "url": s["url"],
        }

    events = []
    if not first_run:
        cur_ids = set(cur_snapshot.keys())
        prev_ids = set(prev_snapshot.keys())

        # ===== 出现检测（上新/新色/补货回归）——数据可信，总是安全 =====
        for sid in sorted(cur_ids - prev_ids):
            info = cur_snapshot[sid]
            ever = sid in seen_skus
            etype = "RESTOCK" if ever else "NEW"
            events.append({
                "type": etype, "product_name": info["name"],
                "sku": info["variant"] or sid, "number": info["goods_id"],
                "url": info["url"], "image": info["image"],
                "time": datetime.now().isoformat(),
            })
            logger.info(f"{'📦 补货' if ever else '🆕 上新'}: {info['name']} - {info['variant']}")

        # ===== 消失检测（卖空/下架）——半页失败防线 =====
        ratio = len(cur_ids) / max(len(prev_ids), 1)
        if ratio < SNAPSHOT_MIN_RATIO:
            logger.warning(f"本轮SKU数仅为上轮 {ratio:.0%}，疑似抓取不完整，跳过卖空检测")
        else:
            for sid in sorted(prev_ids - cur_ids):
                info = prev_snapshot[sid]
                events.append({
                    "type": "SOLD_OUT", "product_name": info["name"],
                    "sku": info["variant"] or sid, "number": info["goods_id"],
                    "url": info["url"], "image": info["image"],
                    "time": datetime.now().isoformat(),
                })
                logger.info(f"🚨 卖空: {info['name']} - {info['variant']}")

    logger.info(f"本轮扫描完成: {len(cur_snapshot)} 个SKU, {len(events)} 个变化")

    if first_run:
        logger.info(f"基线建立完成: {len(cur_snapshot)} 个SKU已记录，下一轮开始正常监控")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")

    # 保存状态：快照 + 历史见过集合（回归检测用）
    for sid in cur_snapshot:
        seen_skus[sid] = datetime.now().strftime("%Y-%m-%d")
    new_state = {"_snapshot": cur_snapshot, "_seen_skus": seen_skus}
    save_state(new_state)
    logger.info(f"状态已保存（历史 SKU {len(seen_skus)} 个）")

if __name__ == "__main__":
    main()
