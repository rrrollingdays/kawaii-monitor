#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第五轮探测：分类页/搜索页能否访问并列出全部商品"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def clip(x, n=1200):
    return re.sub(r"\s+", " ", x)[:n]

def get(url):
    try:
        r = s.get(url, impersonate="chrome124", timeout=25)
        blocked = "Just a moment" in r.text[:8000]
        print(f"GET {url} -> {r.status_code} len={len(r.text)} blocked={blocked}")
        return r.text if (r.status_code == 200 and not blocked) else ""
    except Exception as e:
        print(f"GET {url} ERR {str(e)[:90]}")
        return ""

home = get(BASE + "/")
# 1. 收集所有 genre_id
genres = sorted(set(re.findall(r'genre_id[=:"]+(\d+)', home)))
print("[首页出现的 genre_id]", genres)

# 2. GET 分类页（多试几个）
for gid in genres[:4]:
    body = get(f"{BASE}/index.php?genre_id={gid}&lang_id=en&tsuka_conv=USD")
    if body:
        cards = re.findall(r'href="https://tokyokawaiilife\.com/goods_en_USD_(\d+)\.html"', body)
        sold = body.count("SOLD OUT")
        print(f"  genre {gid}: 商品卡片 {len(cards)} 个, SOLD OUT {sold} 处")
        # 翻页控件
        pager = re.findall(r'(page|p|offset|start)[=_"]+(\d+)', body)[:8]
        print("  翻页参数候选:", pager)
        # 搜索结果卡片结构片段
        m = re.search(r'goods_en_USD_\d+\.html.{0,1200}', body, re.S)
        if m and gid == genres[0]:
            print("  [分类页卡片片段]\n", clip(m.group(0), 1500))
        break

# 3. POST 搜索（keyword 留空 = 全部？）
for gid in [None] + genres[:2]:
    data = {"lang_id": "en", "act": "search", "tsuka_conv": "USD", "keyword": ""}
    if gid:
        data["genre_id"] = gid
    try:
        r = s.post(BASE + "/", data=data, impersonate="chrome124", timeout=25)
        blocked = "Just a moment" in r.text[:8000]
        n = len(re.findall(r'goods_en_USD_(\d+)\.html', r.text)) if not blocked else 0
        sold = r.text.count("SOLD OUT") if not blocked else 0
        print(f"POST search genre={gid} -> {r.status_code} len={len(r.text)} blocked={blocked} 卡片={n} SOLDOUT={sold}")
        if not blocked and n:
            ids = sorted(set(re.findall(r'goods_en_USD_(\d+)\.html', r.text)))
            print("  商品ID样例:", ids[:15], "... 共", len(ids))
            m = re.search(r'(検索結果|search result|results?|件)', r.text)
            print("  结果数字:", clip(m.group(0) if m else "?", 200))
            break
    except Exception as e:
        print(f"POST search ERR {str(e)[:90]}")
