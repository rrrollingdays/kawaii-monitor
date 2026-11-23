#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 andmary Shopify 接口：collections/all 可用性、variant sku 字段、全站商品总数"""
import json, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

def fetch(url, retries=3):
    for a in range(1, retries + 1):
        try:
            req = Request(url, headers=H)
            with urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            print(f"HTTP {e.code} ({a}/{retries}) {url}")
            if a < retries:
                time.sleep(8 * a)
            else:
                raise

d = fetch("https://andmary.co.jp/collections/all/products.json?limit=3&page=1")
print("collections/all OK, 本页:", len(d["products"]))
for p in d["products"][:2]:
    print(f"handle: {p['handle']}  title: {p['title']}")
    for v in p["variants"][:3]:
        print("   ", json.dumps({k: v.get(k) for k in ["title", "option1", "sku", "barcode", "available", "price", "compare_at_price"]}, ensure_ascii=False))

# 统计全站商品总数
total = 0
page = 1
sample = None
while True:
    d = fetch(f"https://andmary.co.jp/collections/all/products.json?limit=250&page={page}")
    batch = d.get("products", [])
    if sample is None:
        for p in batch:
            if len(p["variants"]) >= 2:
                sample = p
    total += len(batch)
    if len(batch) < 250:
        break
    page += 1
    time.sleep(1)
print(f"全站商品总数: {total}")
if sample:
    print(f"多variant样例: {sample['handle']} {sample['title']}")
    for v in sample["variants"][:4]:
        print("   ", json.dumps({k: v.get(k) for k in ["title", "option1", "sku", "available"]}, ensure_ascii=False))
