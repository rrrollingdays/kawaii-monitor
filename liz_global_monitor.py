#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LIZ LISA 海外官网 (tokyokawaiilife.com, multilingualcart 平台) 卖空/补货/上新/折扣监控
- Cloudflare 防护：必须用 curl_cffi 浏览器指纹（商品详情页被硬拦，走分类列表页监控）
- 数据源：index.php?genre_id=N&offset=X 分类列表页（商品卡片自带 SOLD OUT 标记）
- 粒度：商品级（海外站不暴露颜色级库存）
- 通知标题带 [LIZ海外]，与日本站 [lizlisa] 区分
- 首轮运行仅建立基线，不推送任何通知
"""
import sys, os, re, json, smtplib, logging, ssl, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

BASE = "https://tokyokawaiilife.com"
GENRE_URL = BASE + "/index.php?genre_id={genre}&offset={offset}&lang_id=en&tsuka_conv=USD"
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
PAGE_DELAY = 2.5          # 翻页间隔（秒），CF 站点礼貌抓取
MAX_PAGES_PER_GENRE = 8   # 单分类翻页上限（每页 30 个商品，兜底）
REQUEST_TIMEOUT = 25
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/liz_global_state.json")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")

# ======================== 抓取（curl_cffi 过 Cloudflare） ========================
_session = None
def get_session():
    global _session
    if _session is None:
        from curl_cffi import requests as cf_requests
        _session = cf_requests.Session()
    return _session

def fetch_page(url, retries=3, base_delay=10):
    """curl_cffi 指纹抓取，403/429 退避重试；被 CF 拦截视为失败"""
    s = get_session()
    for attempt in range(1, retries + 1):
        try:
            r = s.get(url, impersonate="chrome124", timeout=REQUEST_TIMEOUT)
            body = r.text
            if r.status_code == 200 and "Just a moment" not in body[:8000]:
                return body
            logger.warning(f"HTTP {r.status_code}{'(CF拦截)' if 'Just a moment' in body[:8000] else ''}，{base_delay * 2 ** (attempt - 1)}s 后重试 ({attempt}/{retries}): {url}")
        except Exception as e:
            logger.warning(f"抓取异常: {e}，重试 ({attempt}/{retries})")
        if attempt < retries:
            time.sleep(base_delay * 2 ** (attempt - 1))
    raise RuntimeError(f"抓取失败（重试后仍失败）: {url}")

# ======================== 解析 ========================
# 卡片统一结构：<a href="goods_en_USD_{id}.html"> ... </a> 内含
#   名称 <p><span>...</span></p>、价格 <p class="re_en"><b>17,000&nbsp;JPY</b>...、
#   卖空标记 <span ...>SOLD OUT</span>（仅卖空时出现）
# 首页轮播等纯图片链接无名称/价格，自动跳过
LINK_RE = re.compile(
    r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html">(.*?)</a>', re.S)
PRICE_RE = re.compile(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>')
SOLD_RE = re.compile(r'>\s*SOLD\s*OUT\s*<')

def parse_cards(html):
    """解析页面所有商品卡片 → {goods_id: {...}}；返回 None 表示页面无效"""
    products = {}
    for m in LINK_RE.finditer(html):
        gid, inner = m.group(1), m.group(2)
        # 名称：第一个 <p><span>...</span></p>，去掉 HTML 注释（COMING SOON 标记等）
        nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
        if not nm:
            continue   # 纯图片链接（轮播等），跳过
        name = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
        if not name:
            continue
        pm = PRICE_RE.search(inner)
        if not pm:
            continue   # 无价格的卡片（异常/占位），跳过
        price = int(pm.group(1).replace(",", ""))
        sold = bool(SOLD_RE.search(inner))
        im = re.search(r'src="(https://cdn\.multilingualcart\.com/[^"\']*goods_img/goods_' + gid + r'[^"\']*)"', inner)
        products[gid] = {
            "name": name,
            "price": price,
            "sold_out": sold,
            "image": im.group(1) if im else "",
        }
    return products

# COMING SOON 预告商品（goodslist_box 区块：有名称无价格，尚未开卖）：
# <a href="goods_en_USD_2651.html"><img ...></a> <span class="gd_name"> <a href="..."> <!-- COMING SOON--> 名称</a>
CS_RE = re.compile(
    r'goods_en_USD_(\d+)\.html"><img[^>]*></a>\s*<span class="gd_name">\s*<a href="[^"]*">'
    r'(?:\s*<!--.*?-->)?\s*(.*?)</a>', re.S)

def parse_coming_soon(html):
    """解析 COMING SOON 预告商品 → {goods_id: name}"""
    out = {}
    for m in CS_RE.finditer(html):
        gid = m.group(1)
        name = re.sub(r"<!--.*?-->", "", m.group(2), flags=re.S).strip()
        if gid not in out and name:
            out[gid] = name
    return out

# ======================== 商品发现与全量扫描 ========================
def discover_genres():
    """从首页收集所有分类 genre_id（缓存于 state 亦可，简单起见每轮重新发现）"""
    home = fetch_page(BASE + "/")
    genres = sorted(set(re.findall(r'genre_id[=:"]+(\d+)', home)))
    return genres or ["17", "18", "38", "70", "8"]

def scan_all():
    """扫描所有分类页 → (商品卡片 {gid: info}, COMING SOON 预告 {gid: name})。
    任一分类失败则抛异常（本轮放弃，保留旧状态）"""
    genres = discover_genres()
    logger.info(f"发现分类: {genres}")
    all_products = {}
    all_coming_soon = {}
    for genre in genres:
        got_any = False
        for offset in range(0, MAX_PAGES_PER_GENRE * 30, 30):
            html = fetch_page(GENRE_URL.format(genre=genre, offset=offset))
            cards = parse_cards(html)
            if cards is None:
                raise RuntimeError(f"分类页解析失败 genre={genre} offset={offset}")
            for gid, name in parse_coming_soon(html).items():
                all_coming_soon.setdefault(gid, name)
            new_cnt = 0
            for gid, info in cards.items():
                if gid not in all_products:
                    all_products[gid] = info
                    new_cnt += 1
            if new_cnt == 0 and offset > 0:
                break   # 本页无新商品，翻页结束
            got_any = True
            if offset + 30 >= MAX_PAGES_PER_GENRE * 30:
                break
            time.sleep(PAGE_DELAY)
        if got_any:
            time.sleep(PAGE_DELAY)
    return all_products, all_coming_soon

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

def _goods_url(gid):
    return f"{BASE}/goods_en_USD_{gid}.html"

def _event_desp(events, heading):
    desp = f"### {heading}\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- {e.get('extra', '')}\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    return desp

def _rows(events, color, value_html_fn):
    rows = ""
    for e in events:
        img_html = f'<img src="{e["image"]}" style="max-width:120px;max-height:150px;border:1px solid #ddd;">' if e.get("image") else ""
        rows += f'<tr><td style="padding:8px;border:1px solid #ddd;">{img_html}</td><td style="padding:8px;border:1px solid #ddd;">{e["product_name"]}</td><td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold;">{value_html_fn(e)}</td><td style="padding:8px;border:1px solid #ddd;"><a href="{e["url"]}">查看</a></td></tr>'
    return rows

def _notify_soldout(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🚨 [LIZ海外] 卖空: {e['product_name']}"
        title = f"[LIZ海外]卖空:{e['product_name'][:15]}"
    else:
        subject = f"🚨 [LIZ海外] {len(events)} 个商品卖空"
        title = f"[LIZ海外]{len(events)}个卖空"
    rows = _rows(events, "#e74c3c", lambda e: "SOLD OUT")
    body = f'<html><body><h2 style="color:#e74c3c;">🚨 [LIZ海外] 商品卖空告警</h2><p>{len(events)} 个商品卖空:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    send_email(subject, body)
    send_wechat(title, _event_desp(events, "[LIZ海外] 卖空告警"))
    send_bark(title, _event_desp(events, "[LIZ海外] 卖空告警"))

def _notify_restock(events):
    if len(events) == 1:
        e = events[0]
        subject = f"📦 [LIZ海外] 补货: {e['product_name']}"
        title = f"[LIZ海外]补货:{e['product_name'][:15]}"
    else:
        subject = f"📦 [LIZ海外] {len(events)} 个商品补货"
        title = f"[LIZ海外]{len(events)}个补货"
    rows = _rows(events, "#27ae60", lambda e: "有货")
    body = f'<html><body><h2 style="color:#27ae60;">📦 [LIZ海外] 补货通知</h2><p>{len(events)} 个商品已补货上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    send_email(subject, body)
    send_wechat(title, _event_desp(events, "[LIZ海外] 补货通知"))
    send_bark(title, _event_desp(events, "[LIZ海外] 补货通知"))

def _notify_new(events):
    if len(events) == 1:
        e = events[0]
        subject = f"🆕 [LIZ海外] 上新: {e['product_name']}"
        title = f"[LIZ海外]上新:{e['product_name'][:15]}"
    else:
        subject = f"🆕 [LIZ海外] {len(events)} 个商品上新"
        title = f"[LIZ海外]{len(events)}个上新"
    rows = _rows(events, "#e67e22", lambda e: e.get("extra", "上架"))
    body = f'<html><body><h2 style="color:#e67e22;">🆕 [LIZ海外] 上新通知</h2><p>{len(events)} 个新商品上架:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    send_email(subject, body)
    send_wechat(title, _event_desp(events, "[LIZ海外] 上新通知"))
    send_bark(title, _event_desp(events, "[LIZ海外] 上新通知"))

def _notify_price(events, up):
    icon, color, word = ("📈", "#27ae60", "回调") if up else ("📉", "#c0392b", "折扣")
    if len(events) == 1:
        e = events[0]
        subject = f"{icon} [LIZ海外] {word}: {e['product_name']} {e['discount']}"
        title = f"[LIZ海外]{word}:{e['product_name'][:15]} {e['discount']}"
    else:
        subject = f"{icon} [LIZ海外] {len(events)} 个商品{word}"
        title = f"[LIZ海外]{len(events)}个{word}"
    rows = _rows(events, color, lambda e: f"¥{e['new_price']:,}（{e['discount']}）")
    heading = "[LIZ海外] 价格回调" if up else "[LIZ海外] 限时折扣"
    body = f'<html><body><h2 style="color:{color};">{icon} {heading}</h2><p>{len(events)} 个商品价格变动:</p><table style="border-collapse:collapse;">{rows}</table></body></html>'
    desp = f"### {heading}\n\n"
    for e in events:
        desp += f"**{e['product_name']}**\n- {e.get('extra', '')}→ **¥{e['new_price']:,}**（{e['discount']}）\n- [查看商品]({e['url']})\n"
        if e.get("image"):
            desp += f"<img src=\"{e['image']}\" width=\"220\"><br>\n"
        desp += "\n"
    send_email(subject, body)
    send_wechat(title, desp)
    send_bark(title, desp)

def notify_events(events):
    if not events:
        return
    groups = {
        "NEW": _notify_new,
        "SOLD_OUT": _notify_soldout,
        "RESTOCK": _notify_restock,
        "SALE": lambda es: _notify_price(es, False),
        "PRICE_UP": lambda es: _notify_price(es, True),
    }
    for etype, fn in groups.items():
        es = [e for e in events if e["type"] == etype]
        if es:
            fn(es)

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
        url = _goods_url("2684")
        notify_events([
            {"type": "SOLD_OUT", "product_name": "【测试-卖空】Fur Tiered Skirt-Pants", "url": url, "image": "", "extra": "JPY 11,000", "time": datetime.now().isoformat()},
            {"type": "RESTOCK", "product_name": "【测试-补货】Fur Tiered Skirt-Pants", "url": url, "image": "", "extra": "JPY 11,000", "time": datetime.now().isoformat()},
            {"type": "NEW", "product_name": "【测试-上新】Fur Tiered Skirt-Pants", "url": url, "image": "", "extra": "JPY 11,000", "time": datetime.now().isoformat()},
            {"type": "SALE", "product_name": "【测试-折扣】Fur Tiered Skirt-Pants", "url": url, "image": "", "new_price": 7700, "discount": "-30%", "extra": "JPY 11,000 → ", "time": datetime.now().isoformat()},
        ])
        return

    try:
        products, coming_soon = scan_all()
    except Exception as e:
        logger.error(f"本轮扫描失败（保留旧状态，不产生任何事件）: {e}")
        return
    if not products:
        logger.warning("未获取到商品列表，本轮跳过，保留旧状态")
        return

    prev = load_state()
    prev_products = {k: v for k, v in prev.items() if not k.startswith("_")}
    prev_cs = prev.get("_coming_soon", {})
    is_baseline = not prev_products

    events = []
    for gid, info in products.items():
        url = _goods_url(gid)
        prev_info = prev_products.get(gid)
        if prev_info is None:
            if not is_baseline:
                # 曾以 COMING SOON 预告过的商品上架 = 上新（预告名可补充显示）
                cs_note = f"（预告名: {prev_cs[gid]}）" if gid in prev_cs else ""
                events.append({"type": "NEW", "product_name": info["name"] + cs_note, "url": url, "image": info["image"], "extra": f"JPY {info['price']:,}", "time": datetime.now().isoformat()})
                logger.info(f"🆕 上新: {info['name']} {cs_note}")
            continue
        # 卖空/补货（商品级）
        if info["sold_out"] and not prev_info.get("sold_out"):
            events.append({"type": "SOLD_OUT", "product_name": info["name"], "url": url, "image": info["image"], "extra": f"JPY {info['price']:,}", "time": datetime.now().isoformat()})
            logger.info(f"🚨 卖空: {info['name']}")
        elif not info["sold_out"] and prev_info.get("sold_out"):
            events.append({"type": "RESTOCK", "product_name": info["name"], "url": url, "image": info["image"], "extra": f"JPY {info['price']:,}", "time": datetime.now().isoformat()})
            logger.info(f"📦 补货: {info['name']}")
        # 折扣/回调（商品级价格，JPY）
        old_price = prev_info.get("price", 0)
        new_price = info["price"]
        if new_price and old_price and new_price != old_price:
            if new_price > old_price:
                rise = round((new_price / old_price - 1) * 100)
                if rise >= 2:
                    events.append({"type": "PRICE_UP", "product_name": info["name"], "url": url, "image": info["image"], "new_price": new_price, "discount": f"+{rise}%", "extra": f"JPY {old_price:,} → ", "time": datetime.now().isoformat()})
                    logger.info(f"📈 回调: {info['name']} ¥{old_price:,}→¥{new_price:,}")
            else:
                drop = round((1 - new_price / old_price) * 100)
                if drop >= 2:
                    events.append({"type": "SALE", "product_name": info["name"], "url": url, "image": info["image"], "new_price": new_price, "discount": f"-{drop}%", "extra": f"JPY {old_price:,} → ", "time": datetime.now().isoformat()})
                    logger.info(f"📉 折扣: {info['name']} ¥{old_price:,}→¥{new_price:,}")

    logger.info(f"本轮扫描完成: {len(products)} 个在售商品, {len(coming_soon)} 个 COMING SOON 预告, {len(events)} 个变化")

    # 已下架商品保留旧状态（分类页翻页抖动时不误报为消失）
    final_state = {k: v for k, v in prev.items() if k.startswith("_")}
    for gid, info in products.items():
        final_state[gid] = info
    for gid, old in prev_products.items():
        if gid not in products:
            final_state[gid] = old
            logger.info(f"本轮未扫到，保留旧状态: {old.get('name', gid)}")
    final_state["_coming_soon"] = coming_soon
    save_state(final_state)

    if is_baseline:
        logger.info(f"首轮基线建立完成: {len(products)} 个商品已记录（另 {len(coming_soon)} 个预告），本轮不推送任何通知")
    elif events:
        notify_events(events)
    else:
        logger.info("本轮无变化")
    logger.info(f"状态已保存（共 {len(final_state)} 个商品）")

if __name__ == "__main__":
    main()
