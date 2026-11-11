#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第十二轮探测：找完整分类/AJAX接口，查清真实商品总数（用户称244个）"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()
home = s.get(BASE + "/", impersonate="chrome124", timeout=25).text
print("首页 ok:", "Just a moment" not in home[:8000], "len:", len(home))

# 1. 所有 <script src> 外链 JS
print("\n[外链 JS]")
for m in dict.fromkeys(re.findall(r'<script[^>]*src="([^"]+)"', home)):
    print("  ", m[:150])

# 2. 页内 AJAX/fetch 调用
print("\n[页内 ajax/fetch 线索]")
for kw in ["$.ajax", "$.get", "$.post", "fetch(", "XMLHttpRequest", ".json", "/api/"]:
    for m in list(re.finditer(re.escape(kw), home))[:4]:
        print(f"  [{kw}]", re.sub(r"\s+", " ", home[max(0,m.start()-120):m.end()+250])[:350])

# 3. 所有 genre_id 引用（这次统计全部出现形式）
genres = re.findall(r'genre_id[=("&\']+(\d+)', home)
from collections import Counter
print("\n[首页全部 genre_id 出现统计]", Counter(genres))

# 4. 搜索表单完整结构（可能有分类下拉）
for m in list(re.finditer(r'<form[^>]*act=search[^>]*>.*?</form>', home, re.S))[:2]:
    print("\n[搜索表单]\n", re.sub(r"\s+", " ", m.group(0))[:800])
for m in list(re.finditer(r'<select[^>]*>.*?</select>', home, re.S))[:5]:
    print("\n[select 下拉]\n", re.sub(r"\s+", " ", m.group(0))[:600])

# 5. POST 搜索页同样检查 genre 下拉和 AJAX
r = s.post(BASE + "/", data={"lang_id": "en", "act": "search", "tsuka_conv": "USD", "keyword": "", "genre_id": ""}, impersonate="chrome124", timeout=25)
body = r.text
print("\nPOST 搜索页 len:", len(body))
gs = Counter(re.findall(r'genre_id[=("&\']+(\d+)', body))
print("[搜索页 genre_id 统计]", gs)
for m in list(re.finditer(r'<select[^>]*>.*?</select>', body, re.S))[:5]:
    print("\n[搜索页 select]\n", re.sub(r"\s+", " ", m.group(0))[:800])
# 搜索结果页的"件数/结果"区（可能带 244 之类数字）
tail = re.sub(r"<[^>]+>", " ", body)
nums = re.findall(r'\b\d{2,4}\b', tail)
print("[搜索页正文数字候选]", sorted(set(int(n) for n in nums if 100 <= int(n) <= 500))[:30])

# 6. 搜索页里 sch_goods_tag（搜索表单隐藏字段）相关
for m in list(re.finditer(r'sch_goods_tag', body))[:3]:
    print("\n[sch_goods_tag 上下文]", re.sub(r"\s+", " ", body[max(0,m.start()-150):m.end()+150])[:300])
