#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第九轮探测：POST 搜索逐页翻页，查清全站商品总数与翻页机制"""
import re, time
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def parse_cards(body):
    cards = {}
    for m in re.finditer(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html">(.*?)</a>', body, re.S):
        gid, inner = m.group(1), m.group(2)
        nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
        if not nm: continue
        name = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
        pm = re.search(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>', inner)
        if not name or not pm: continue
        cards[gid] = {"name": name, "sold": bool(re.search(r'>\s*SOLD\s*OUT\s*<', inner))}
    return cards

# 1. 模拟真实用户：先 GET 首页拿 cookie
home = s.get(BASE + "/", impersonate="chrome124", timeout=25).text
print("首页 ok:", "Just a moment" not in home[:8000])

# 2. POST 搜索（keyword 空 = 全部）
r = s.post(BASE + "/", data={"lang_id": "en", "act": "search", "tsuka_conv": "USD", "keyword": "", "genre_id": ""}, impersonate="chrome124", timeout=25)
body = r.text
print("POST search:", r.status_code, "blocked:", "Just a moment" in body[:8000], "len:", len(body))

# 3. 总件数文本（搜索结果页通常有 "N件" 或 "N items"）
for m in re.finditer(r'>([^<>]{0,40}?[\d,]+\s*件[^<>]{0,20})<', body):
    print("[件数]", m.group(1).strip())
for m in re.finditer(r'([\d,]+)\s*(items?|results?|products?)', body, re.I):
    print("[items 文本]", m.group(0), "上下文:", re.sub(r"\s+", " ", body[max(0,m.start()-100):m.end()+50])[:180])

# 4. 翻页控件原样
for m in list(re.finditer(r'<a[^>]+href="([^"]*)"[^>]*>\s*(?:&gt;|next|次へ|&rsaquo;|>\d+<)', body, re.I))[:6]:
    print("[翻页链接]", m.group(1)[:130])
# 所有含 offset/page 的链接
for m in list(re.finditer(r'href="([^"]*(?:offset|page|start)[^"]*)"', body))[:8]:
    print("[offset/page 链接]", m.group(1)[:130])

# 5. 逐页收集（尝试 offset=0,30,60...用 POST 方式）
seen = {}
for off in [0, 30, 60, 90, 120, 150]:
    r = s.post(BASE + "/", data={"lang_id": "en", "act": "search", "tsuka_conv": "USD", "keyword": "", "genre_id": "", "offset": str(off)}, impersonate="chrome124", timeout=25)
    if r.status_code != 200 or "Just a moment" in r.text[:8000]:
        print(f"POST off{off}: BLOCKED"); continue
    cards = parse_cards(r.text)
    new = set(cards) - set(seen)
    seen.update(cards)
    print(f"POST off{off}: 卡片{len(cards)} 新{len(new)} 累计{len(seen)}")
    time.sleep(2)

# 6. GET 搜索形式翻页（act=search 放 query）
for off in [0, 30, 60]:
    r = s.get(f"{BASE}/?lang_id=en&act=search&tsuka_conv=USD&keyword=&genre_id=&offset={off}&_={int(time.time()*1000)}", impersonate="chrome124", timeout=25)
    if r.status_code != 200 or "Just a moment" in r.text[:8000]:
        print(f"GET-search off{off}: BLOCKED"); continue
    cards = parse_cards(r.text)
    new = set(cards) - set(seen)
    seen.update(cards)
    print(f"GET-search off{off}: 卡片{len(cards)} 新{len(new)} 累计{len(seen)}")
    time.sleep(2)

print("\n最终累计可解析商品:", len(seen))
sold = [g for g, v in seen.items() if v["sold"]]
print("其中 SOLD OUT:", len(sold), sold[:10])
