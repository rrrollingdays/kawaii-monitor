#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第八轮探测：cache-buster 时间戳参数能否绕过 CF 缓存 + 确认翻页"""
import re, time
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def fetch(url):
    r = s.get(url, impersonate="chrome124", timeout=25)
    ok = r.status_code == 200 and "Just a moment" not in r.text[:8000]
    return r.text if ok else ""

# A. 不带 buster 连抓两次同一 URL（对照）
u = f"{BASE}/index.php?genre_id=8&offset=30&lang_id=en&tsuka_conv=USD"
a1, a2 = fetch(u), fetch(u)
if a1 and a2:
    print("A 无buster 两次一致:", a1 == a2, "ID数:", len(set(re.findall(r'goods_en_USD_(\d+)', a1))), len(set(re.findall(r'goods_en_USD_(\d+)', a2))))

# B. 带 buster 抓相同 URL
b1 = fetch(u + f"&_={int(time.time())}")
b2 = fetch(u + f"&_={int(time.time())+1}")
if b1 and b2:
    i1 = set(re.findall(r'goods_en_USD_(\d+)', b1)); i2 = set(re.findall(r'goods_en_USD_(\d+)', b2))
    print("B 带buster ID数:", len(i1), len(i2), "两次一致:", b1 == b2, "与无buster差异:", len(i1 ^ set(re.findall(r'goods_en_USD_(\d+)', a1))))

# C. 带 buster 翻页 genre8 offset=0/30/60/90
seen = set()
for off in [0, 30, 60, 90, 120]:
    body = fetch(f"{BASE}/index.php?genre_id=8&offset={off}&lang_id=en&tsuka_conv=USD&_={int(time.time()*1000)}")
    if not body:
        print(f"C off{off}: BLOCKED"); continue
    # 统计"列表卡片"（有名称有价格）
    cards = {}
    for m in re.finditer(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html">(.*?)</a>', body, re.S):
        gid, inner = m.group(1), m.group(2)
        nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
        if not nm: continue
        nm2 = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
        pm = re.search(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>', inner)
        if not nm2 or not pm: continue
        cards[gid] = nm2[:40]
    new = set(cards) - seen
    seen |= set(cards)
    print(f"C off{off}: 卡片{len(cards)} 新{len(new)} 累计{len(seen)}  样例: {list(new)[:5]}")
    time.sleep(2)
print("C 全站可解析商品总数:", len(seen))
