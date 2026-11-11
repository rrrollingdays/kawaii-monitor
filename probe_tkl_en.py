#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第十轮探测：CF 缓存机制定位与翻页绕过实验"""
import re, time
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"

def parse_cards(body):
    cards = {}
    for m in re.finditer(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html">(.*?)</a>', body, re.S):
        gid, inner = m.group(1), m.group(2)
        nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S)
        if not nm: continue
        name = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
        pm = re.search(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>', inner)
        if not name or not pm: continue
        cards[gid] = name[:30]
    return cards

def ids_of(body):
    return set(re.findall(r'goods_en_USD_(\d+)\.html', body))

# 新 session
s = requests.Session()
s.get(BASE + "/", impersonate="chrome124", timeout=25)

U30 = f"{BASE}/index.php?lang_id=en&act=search&tsuka_conv=USD&keyword=&genre_id=&offset=30"

# A. 同 URL 连抓 2 次
a1 = s.get(U30, impersonate="chrome124", timeout=25).text
a2 = s.get(U30, impersonate="chrome124", timeout=25).text
print("A 同URL两次: 一致", a1 == a2, "| ID:", len(ids_of(a1)), len(ids_of(a2)), "| 卡片:", len(parse_cards(a1)), len(parse_cards(a2)))

# B. 不同 query 顺序 / 加垃圾参数
b1 = s.get(U30 + "&zzz=123", impersonate="chrome124", timeout=25).text
b2 = s.get(f"{BASE}/index.php?tsuka_conv=USD&lang_id=en&act=search&genre_id=&keyword=&offset=30", impersonate="chrome124", timeout=25).text
print("B 加参数/换序: 与A一致?", b1 == a1, b2 == a1, "| ID:", len(ids_of(b1)), len(ids_of(b2)))

# C. POST + query 分页组合
c1 = s.post(U30, data={"lang_id": "en", "act": "search", "tsuka_conv": "USD", "keyword": "", "genre_id": ""}, impersonate="chrome124", timeout=25).text
print("C POST+query offset=30: ID:", len(ids_of(c1)), "卡片:", len(parse_cards(c1)))

# D. 带假 cookie 绕缓存
d1 = s.get(U30, impersonate="chrome124", timeout=25, headers={"Cookie": "cachebust=1; __cf_bm=x"}).text
print("D 带假cookie: ID:", len(ids_of(d1)), "卡片:", len(parse_cards(d1)), "与A一致?", d1 == a1)

# E. 全文 ID 上下文分类（58/120 个 ID 里多少在 <a> 卡片内）
all_ids = ids_of(a1)
in_a = set(m.group(1) for m in re.finditer(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html', a1))
print("E 页面唯一ID:", len(all_ids), "| 在<a>内:", len(in_a), "| 不在<a>内:", sorted(all_ids - in_a)[:15])

# F. sleep 90s 后再抓（等缓存过期？）
time.sleep(90)
f1 = s.get(U30, impersonate="chrome124", timeout=25).text
print("F 90s后再抓: 与A一致?", f1 == a1, "| ID:", len(ids_of(f1)), "卡片:", len(parse_cards(f1)))

# G. 如果 F 有变化，继续 off60
g1 = s.get(U30.replace("offset=30", "offset=60"), impersonate="chrome124", timeout=25).text
print("G off60: ID:", len(ids_of(g1)), "卡片:", len(parse_cards(g1)), "样例:", list(parse_cards(g1))[:8])
