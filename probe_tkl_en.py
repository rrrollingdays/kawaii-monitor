#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第十四轮探测：goodslist_box 完整结构 + 全站 244 商品解析预览"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def fetch(off):
    r = s.get(f"{BASE}/index.php?lang_id=en&genre_id=&keyword=&sch_goods_tag=&act=search&sortkey=&offset={off}&tsuka_conv=USD", impersonate="chrome124", timeout=25)
    return r.text

b0 = fetch(0)
# 打印第一个 goodslist_box 块完整 HTML（到下一个块开始）
parts = re.split(r'(?=<div class="goodslist_box")', b0)
boxes = [p for p in parts if p.startswith('<div class="goodslist_box"')]
print(f"off0 goodslist_box 块数: {len(boxes)}")
print("\n[第1块完整HTML]\n", re.sub(r"\s+", " ", boxes[0][:2600])[:2600])
print("\n[第2块完整HTML]\n", re.sub(r"\s+", " ", boxes[1][:1800])[:1800] if len(boxes) > 1 else "")

# 找一个带 SOLD OUT 或 セール 的块
for b in boxes:
    if "SOLD OUT" in b or "セール" in b:
        print("\n[带SOLDOUT/セール块]\n", re.sub(r"\s+", " ", b[:2600])[:2600])
        break

# 全站翻页解析预览
def parse_block(b):
    gid_m = re.search(r'goods_en_USD_(\d+)\.html', b)
    if not gid_m:
        return None
    gid = gid_m.group(1)
    nm = re.search(r'<span class="gd_name">\s*<a href="[^"]*">\s*(.*?)</a>', b, re.S)
    if not nm:
        return None
    name = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
    prices = re.findall(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>', b)
    sold = bool(re.search(r'>\s*SOLD\s*OUT\s*<', b))
    coming = "COMING SOON" in b
    im = re.search(r'<img src="(https://cdn\.multilingualcart\.com/[^"]*goods_img/goods_' + gid + r'[^"]*)"', b)
    return gid, name, prices, sold, coming, (im.group(1) if im else "")

total = {}
for off in range(0, 270, 30):
    body = fetch(off)
    parts = re.split(r'(?=<div class="goodslist_box")', body)
    boxes = [p for p in parts if p.startswith('<div class="goodslist_box"')]
    pager = re.findall(r'\[\s*([\d,\s]+?)\s*-\s*([\d,\s]+?)\s*\]\s*of\s*\[\s*([\d,]+)\s*\]', body)
    n_ok = 0
    for b in boxes:
        p = parse_block(b)
        if p:
            gid, name, prices, sold, coming, img = p
            total[gid] = (name, prices, sold, coming, img)
            n_ok += 1
    print(f"off{off}: 页码{pager[0] if pager else '?'} 块{len(boxes)} 解析成功{n_ok}")
    import time; time.sleep(2)

print(f"\n全站解析商品总数: {len(total)}")
sold = [g for g, v in total.items() if v[2]]
coming = [g for g, v in total.items() if v[3]]
noprice = [g for g, v in total.items() if not v[1]]
print(f"SOLD OUT: {len(sold)} {sold[:8]}")
print(f"COMING SOON: {len(coming)} {coming[:8]}")
print(f"无价格: {len(noprice)} {noprice[:8]}")
print("\n[商品名样例]")
for g in sorted(total, key=int)[:10]:
    v = total[g]
    print(f"  {g} {'[SO]' if v[2] else ''}{'[CS]' if v[3] else ''} 价格{v[1]} {v[0][:40]}")
