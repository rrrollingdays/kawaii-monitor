#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第七轮探测：用正式解析逻辑统计各页可解析卡片，确认可监控全集"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
LINK_RE = re.compile(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html">(.*?)</a>', re.S)
PRICE_RE = re.compile(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>')

s = requests.Session()
all_ids = set()
for genre in ["8", "17", "18", "38", "70"]:
    for off in [0, 30, 60, 90]:
        r = s.get(f"{BASE}/index.php?genre_id={genre}&offset={off}&lang_id=en&tsuka_conv=USD", impersonate="chrome124", timeout=25)
        if r.status_code != 200 or "Just a moment" in r.text[:8000]:
            print(f"genre{genre} off{off}: BLOCKED/{r.status_code}")
            continue
        body = r.text
        n_link = 0; n_named = 0; n_priced = 0
        ids = set()
        for m in LINK_RE.finditer(body):
            gid, inner = m.group(1), m.group(2)
            n_link += 1
            ids.add(gid)
            nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
            if nm and re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip():
                n_named += 1
            if PRICE_RE.search(inner):
                n_priced += 1
        all_ids |= ids
        print(f"genre{genre} off{off}: 链接{len(ids)}个 有名称{n_named} 有价格{n_priced}")
        # 打印一个无价格但有名称的卡片样例（若有）
        for m in LINK_RE.finditer(body):
            inner = m.group(2)
            nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
            if nm and re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip() and not PRICE_RE.search(inner):
                print("   [无价格但有名称样例]", re.sub(r"\s+", " ", inner)[:400])
                break
print("\n全站有链接引用的商品 ID 总数:", len(all_ids))
