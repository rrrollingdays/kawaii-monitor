#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二轮探测：session 保持 / 多指纹 / 分类页发现"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"

def try_get(s, url, imp=None):
    kw = {"timeout": 25}
    if imp:
        kw["impersonate"] = imp
    try:
        r = s.get(url, **kw)
        body = r.text
        blocked = "Just a moment" in body[:8000] or "Attention Required" in body[:8000]
        print(f"GET {url} -> {r.status_code} len={len(body)} blocked={blocked} (imp={imp or 'session-default'})")
        return None if blocked or r.status_code != 200 else body
    except Exception as e:
        print(f"GET {url} ERR {str(e)[:100]}")
        return None

# --- A. session 首页 -> 商品页（zhcn 与 en 各试一次，多指纹） ---
for imp in ["chrome131", "chrome124", "safari18_0"]:
    s = requests.Session()
    home = try_get(s, BASE + "/", imp)
    if not home:
        continue
    # 分类导航发现
    hrefs = re.findall(r'href="([^"]+)"', home)
    paths = {}
    for h in hrefs:
        m = re.match(r"(?:https?://[^/]+)?/([a-zA-Z_0-9]+)\.(php|html)", h)
        if m:
            paths.setdefault(m.group(1).split("_")[0], h)
    print("首页路径模式:", list(paths.keys())[:30])
    for kind in ["goods_zhcn_USD_2684", "goods_en_USD_2684"]:
        body = try_get(s, f"{BASE}/{kind}.html", imp)
        if body:
            t = re.search(r"<title>(.*?)</title>", body, re.S)
            print(f"  title: {t.group(1).strip()[:80] if t else '?'}")
            # 打印 soldout/库存/select 上下文
            for kw in ["SOLD", "sold_out", "在庫", "stock", "select", "数量", "cart"]:
                i = body.find(kw)
                if i >= 0:
                    print(f"  [{kw}] ...{re.sub(chr(92)+'s+',' ',body[max(0,i-200):i+300])[:420]}")
            open("/tmp/tkl_goods_sample.html", "w").write(body)
            break
    break

# --- B. 不带 session 直接试商品页（对照） ---
for imp in ["chrome131", "safari17_0"]:
    r = requests.get(f"{BASE}/goods_en_USD_2684.html", impersonate=imp, timeout=25)
    blocked = "Just a moment" in r.text[:8000]
    print(f"direct goods_en -> {r.status_code} blocked={blocked} (imp={imp})")
    if r.status_code == 200 and not blocked:
        open("/tmp/tkl_goods_sample.html", "w").write(r.text)
        break
