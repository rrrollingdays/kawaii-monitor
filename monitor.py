#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tokyo Kawaii Life (LIZ LISA) 商品卖空监控程序 — GitHub Actions 版
================================================
配合 GitHub Actions 每5分钟自动运行，发现卖空通过邮件+微信通知。

配置通过环境变量读取（在 GitHub Secrets 中设置）:
    SMTP_USER        Gmail 地址
    SMTP_PASSWORD    Gmail 应用专用密码
    EMAIL_TO         收件邮箱
    SERVERCHAN_KEY   Server酱 SENDKEY
"""

import sys
import os
import re
import json
import smtplib
import logging
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

# ──────────────────────────────────────────────
# 配置（从环境变量读取）
# ──────────────────────────────────────────────
PRODUCT_URLS = [
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6230-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/162-6210-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6041-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6236-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-2013-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6235-0",
    "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6240-0",
]

SMTP_SERVER = "smtp.gmail.com"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")

MAX_WORKERS = 5
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 状态文件用临时目录（GitHub Actions 每次运行是全新环境）
STATE_FILE = "/tmp/monitor_state.json"

# ──────────────────────────────────────────────
# 日志
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor")


# ──────────────────────────────────────────────
# 邮件通知
# ──────────────────────────────────────────────
def send_email(subject, body_html):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        logger.warning("邮件未配置，跳过")
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    text_body = re.sub(r'<[^>]+>', '', body_html)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    context = ssl.create_default_context()
    # STARTTLS (587)
    try:
        with smtplib.SMTP(SMTP_SERVER, 587, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        logger.info(f"邮件已发送: {subject}")
        return True
    except Exception as e1:
        logger.debug(f"STARTTLS失败: {e1}")
    # SSL (465)
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465, context=context, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        logger.info(f"邮件已发送: {subject}")
        return True
    except Exception as e2:
        logger.error(f"邮件发送失败: {e2}")
        return False


# ──────────────────────────────────────────────
# 微信推送
# ──────────────────────────────────────────────
def send_wechat(title, desp):
    if not SERVERCHAN_KEY:
        logger.warning("微信未配置，跳过")
        return False
    title = title[:32]
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    data = urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = Request(url, data=data, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                logger.info(f"微信推送已发送: {title}")
                return True
            else:
                logger.error(f"微信推送失败: {result.get('message', '未知')}")
                return False
    except Exception as e:
        logger.error(f"微信推送失败: {e}")
        return False


# ──────────────────────────────────────────────
# 通知
# ──────────────────────────────────────────────
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

    rows_html = ""
    for e in events:
        rows_html += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;">{e['product_name']}</td>
          <td style="padding:8px;border:1px solid #ddd;color:#e74c3c;font-weight:bold;">{e['sku']}</td>
          <td style="padding:8px;border:1px solid #ddd;">{e['time'][:19].replace('T',' ')}</td>
          <td style="padding:8px;border:1px solid #ddd;"><a href="{e['url']}">查看商品</a></td>
        </tr>"""
    body_html = f"""
    <html><body style="font-family:sans-serif;">
    <h2 style="color:#e74c3c;">🚨 商品卖空告警</h2>
    <p>以下 <b>{len(events)}</b> 个SKU刚刚卖空：</p>
    <table style="border-collapse:collapse;width:100%;">
      <tr style="background:#f8f9fa;">
        <th style="padding:8px;border:1px solid #ddd;">商品名称</th>
        <th style="padding:8px;border:1px solid #ddd;">卖空SKU</th>
        <th style="padding:8px;border:1px solid #ddd;">时间</th>
        <th style="padding:8px;border:1px solid #ddd;">链接</th>
      </tr>
      {rows_html}
    </table>
    <p style="color:#999;margin-top:16px;">Tokyo Kawaii Life 监控程序自动发送</p>
    </body></html>"""

    desp = ""
    for e in events:
        desp += f"**{e['product_name']}**\n- SKU: {e['sku']}\n- 时间: {e['time'][:19]}\n- [查看商品]({e['url']})\n\n"

    send_email(subject, body_html)
    send_wechat(title, desp)


# ──────────────────────────────────────────────
# HTML 解析
# ──────────────────────────────────────────────
def parse_product(html_text):
    name_match = re.search(r'<h1[^>]*class="[^"]*itemTitle[^"]*"[^>]*>(.*?)</h1>',
                           html_text, re.DOTALL)
    product_name = name_match.group(1).strip() if name_match else ""

    num_match = re.search(r'<p[^>]*class="[^"]*itemNumber[^"]*"[^>]*>商品番号\s*　\s*(.*?)</p>',
                          html_text, re.DOTALL)
    product_number = num_match.group(1).strip() if num_match else ""

    table_match = re.search(r'<div[^>]*class="[^"]*FS2_additional_image_tableVariation[^"]*"[^>]*>(.*?)</div>',
                            html_text, re.DOTALL)
    table_html = table_match.group(1) if table_match else ""

    skus = []
    for tr_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr_match.group(1), re.DOTALL)
        if len(tds) < 2:
            continue
        sku_name_raw = tds[0].strip()
        button_html = tds[1].strip()
        sku_name_clean = re.sub(r'<[^>]+>', '', sku_name_raw).strip()
        if not sku_name_clean:
            continue
        is_sold_out = bool(re.search(r'SOLD\s*OUT', sku_name_raw, re.IGNORECASE))
        clean_name = re.sub(r'\s*/?\s*SOLD\s*OUT\s*$', '', sku_name_clean,
                            flags=re.IGNORECASE).strip()
        var_match = re.search(r'name="varno_(\d+_\d+)"', button_html)
        skus.append({
            "name": clean_name,
            "sold_out": is_sold_out,
            "variation_id": var_match.group(1) if var_match else "",
        })

    return {"name": product_name, "number": product_number, "skus": skus}


# ──────────────────────────────────────────────
# 网络请求
# ──────────────────────────────────────────────
def fetch_page(url):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.9",
    })
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    try:
        return raw.decode("Shift_JIS")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


# ──────────────────────────────────────────────
# 状态管理（用 GitHub Actions Cache 持久化）
# ──────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except (OSError, PermissionError):
        pass


# ──────────────────────────────────────────────
# 监控逻辑
# ──────────────────────────────────────────────
def check_product(url):
    try:
        html = fetch_page(url)
        info = parse_product(html)
        return url, info
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        logger.warning(f"抓取失败: {url} → {e}")
        return url, None


def monitor_once(urls, prev_state):
    new_state = {}
    events = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_product, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            _, info = future.result()
            if info is None:
                continue
            current_skus = {sku["name"]: sku["sold_out"] for sku in info["skus"]}
            new_state[url] = {
                "name": info["name"],
                "number": info["number"],
                "skus": current_skus,
                "checked_at": datetime.now().isoformat(),
            }
            prev_skus = prev_state.get(url, {}).get("skus", {})
            for sku_name, is_sold_out in current_skus.items():
                if is_sold_out and not prev_skus.get(sku_name, False):
                    events.append({
                        "type": "SOLD_OUT",
                        "product_name": info["name"],
                        "product_number": info["number"],
                        "sku": sku_name,
                        "url": url,
                        "time": datetime.now().isoformat(),
                    })
    return new_state, events


# ──────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────
def main():
    logger.info(f"监控启动: {len(PRODUCT_URLS)} 个商品")

    # 测试通知模式
    if "--test-notify" in sys.argv:
        logger.info("发送测试通知...")
        notify_events([{
            "type": "SOLD_OUT",
            "product_name": "【测试】シアーフリルセットアップ",
            "product_number": "262-6230-0",
            "sku": "【测试】ピンク(110)",
            "url": "https://www.tokyokawaiilife.jp/fs/lizlisaadmin/262-6230-0",
            "time": datetime.now().isoformat(),
        }])
        logger.info("测试通知发送完毕")
        return

    prev_state = load_state()
    if prev_state:
        logger.info(f"已加载状态 ({len(prev_state)} 个商品)")

    new_state, events = monitor_once(urls=PRODUCT_URLS, prev_state=prev_state)

    # 打印状态
    for url, info in new_state.items():
        name = info.get("name", "???")
        for sku_name, sold_out in info.get("skus", {}).items():
            status = "❌卖空" if sold_out else "✅在售"
            logger.info(f"  {name} - {sku_name}: {status}")

    if events:
        for event in events:
            logger.info(f"🚨 卖空: {event['product_name']} - {event['sku']}")
        notify_events(events)
    else:
        logger.info("本轮无新卖空")

    save_state(new_state)


if __name__ == "__main__":
    main()
