#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 wego.jp whinny×sirotan 联名集合的 products.json 结构"""
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

d = fetch("https://wego.jp/collections/f-cts-whinny-sirotan/products.json?limit=250&page=1")
ps = d["products"]
print(f"集合商品总数: {len(ps)}")
for p in ps[:3]:
    print(f"\nhandle: {p['handle']}  title: {p['title']}  type: {p.get('product_type')}")
    for v in p["variants"][:6]:
        print("   ", json.dumps({k: v.get(k) for k in ["title", "option1", "option2", "sku", "available", "price", "compare_at_price"]}, ensure_ascii=False))

# 统计所有变体与 sku 样式
print("\n--- 全部变体 sku 样式 ---")
for p in ps:
    for v in p["variants"][:2]:
        print(f"{p['handle']:22s} {v.get('sku')}  av={v.get('available')}")
