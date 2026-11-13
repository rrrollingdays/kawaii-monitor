#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 darich 商品 JSON 的 variant sku/barcode 字段"""
import json
from urllib.request import Request, urlopen

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

url = "https://www.darich.shop/collections/all/products.json?limit=5&page=1"
req = Request(url, headers=H)
with urlopen(req, timeout=25) as r:
    d = json.loads(r.read().decode("utf-8"))

for p in d["products"][:3]:
    print(f"handle: {p['handle']}  title: {p['title']}")
    for v in p["variants"][:4]:
        keys = ["title", "option1", "sku", "barcode", "id", "available", "price"]
        print("   ", json.dumps({k: v.get(k) for k in keys}, ensure_ascii=False))
    print()

# 找一个非默认 title 多 variant 的
p = d["products"][-1]
print(f"last: handle={p['handle']} title={p['title']} n_variants={len(p['variants'])}")
for v in p["variants"][:6]:
    keys = ["title", "option1", "sku", "barcode", "id", "available"]
    print("   ", json.dumps({k: v.get(k) for k in keys}, ensure_ascii=False))
