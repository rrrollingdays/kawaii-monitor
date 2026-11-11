#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第十一轮探测：非卡片 ID 上下文 + 首页卡片数对照，确认全站在售数"""
import re
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

home = s.get(BASE + "/", impersonate="chrome124", timeout=25).text
hcards = parse_cards(home)
print("首页: 唯一ID", len(set(re.findall(r'goods_en_USD_(\d+)\.html', home))), "卡片", len(hcards), "其中SOLDOUT", sum(1 for v in hcards.values() if v['sold']))

body = s.get(f"{BASE}/index.php?lang_id=en&act=search&tsuka_conv=USD&keyword=&genre_id=", impersonate="chrome124", timeout=25).text
cards = parse_cards(body)
print("搜索页: 唯一ID", len(set(re.findall(r'goods_en_USD_(\d+)\.html', body))), "卡片", len(cards), "其中SOLDOUT", sum(1 for v in cards.values() if v['sold']))

# 非<a>卡片 ID 的上下文（每个打一段）
in_a = set(m.group(1) for m in re.finditer(r'<a href="' + re.escape(BASE) + r'/goods_en_USD_(\d+)\.html', body))
all_ids = set(re.findall(r'goods_en_USD_(\d+)\.html', body))
extra = sorted(all_ids - in_a, key=int)
print("\n非<a>卡片 ID 数:", len(extra))
for gid in extra[:6]:
    i = body.find(f"goods_en_USD_{gid}.html")
    ctx = re.sub(r"\s+", " ", body[max(0, i-200):i+250])
    print(f"\n--- {gid} @{i}\n{ctx[:420]}")

# 卡片商品名清单（给用户看的实锤）
print("\n[全部在售卡片商品]")
for gid, v in sorted(cards.items(), key=lambda x: int(x[0])):
    print(f"  {gid} {'[SOLDOUT]' if v['sold'] else ''} {v['name'][:45]}")
