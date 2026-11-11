#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第十三轮探测：244 个商品实锤，定位翻页失效原因（重定向？缓存？链接形式？）"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def show(r, tag):
    body = r.text
    pager = re.findall(r'\[\s*([\d,\s]+?)\s*-\s*([\d,\s]+?)\s*\]\s*of\s*\[\s*([\d,]+)\s*\]', body)
    ids = sorted(set(re.findall(r'goods_en_USD_(\d+)\.html', body)), key=int)
    hist = [(h.status_code, h.headers.get("Location", "")[:100]) for h in r.history]
    print(f"{tag}: code={r.status_code} 最终URL={r.url[:120]} 重定向={hist}")
    print(f"   页码指示: {pager[:3]} 唯一ID {len(ids)}  范围 {ids[:5]}...{ids[-5:]}")
    return body

# off0
r0 = s.get(f"{BASE}/index.php?lang_id=en&act=search&tsuka_conv=USD&keyword=&genre_id=&sortkey=", impersonate="chrome124", timeout=25)
b0 = show(r0, "off0 ")

# off30（页面自带翻页链接的原始形式）
r30 = s.get(f"{BASE}/index.php?lang_id=en&genre_id=&keyword=&sch_goods_tag=&act=search&sortkey=&offset=30&tsuka_conv=USD", impersonate="chrome124", timeout=25)
b30 = show(r30, "off30")

# off30 禁止自动重定向，看原始响应
r30raw = s.get(f"{BASE}/index.php?lang_id=en&genre_id=&keyword=&sch_goods_tag=&act=search&sortkey=&offset=30&tsuka_conv=USD", impersonate="chrome124", timeout=25, allow_redirects=False)
print(f"off30 无跟随: code={r30raw.status_code} Location={r30raw.headers.get('Location','')[:150]}")

# 商品一覧区的原始 href 形式对比
for tag, body in [("off0", b0), ("off30", b30)]:
    i = body.find("商品があるときは商品一覧を表示")
    seg = body[i:i+3000] if i > 0 else ""
    hrefs = re.findall(r'href="([^"]*goods_en_USD_\d+\.html[^"]*)"', seg)[:6]
    print(f"[{tag}] 商品一覧区 href 形式: {hrefs}")
    ids_seg = re.findall(r'goods_en_USD_(\d+)\.html', seg)
    print(f"[{tag}] 商品一覧区 ID: {ids_seg[:12]}")

# 全部 href 形式统计（含相对路径）
for tag, body in [("off0", b0), ("off30", b30)]:
    forms = {}
    for m in re.finditer(r'href="([^"]*goods_en_USD_\d+\.html)"', body):
        h = m.group(1)
        key = "absolute" if h.startswith("http") else "relative"
        forms[key] = forms.get(key, 0) + 1
    print(f"[{tag}] href 形式统计: {forms}")

# off30 的列表区完整卡片数（相对+绝对都算）
def parse_all(body):
    out = {}
    for m in re.finditer(r'<a href="[^"]*goods_en_USD_(\d+)\.html[^"]*">(.*?)</a>', body, re.S):
        gid, inner = m.group(1), m.group(2)
        nm = re.search(r'<p>\s*<span[^>]*>(.*?)</span>', inner, re.S) or re.search(r'gd_name[^>]*>\s*<a[^>]*>(.*?)</a>', inner, re.S)
        if not nm: continue
        name = re.sub(r"<!--.*?-->", "", nm.group(1), flags=re.S).strip()
        pm = re.search(r'<b>([\d,]+)\s*(?:&nbsp;)?\s*JPY</b>', inner)
        if not name or not pm: continue
        out[gid] = name[:30]
    return out

c0, c30 = parse_all(b0), parse_all(b30)
print(f"\n宽松解析: off0 卡片 {len(c0)}, off30 卡片 {len(c30)}, 交集 {len(set(c0)&set(c30))}")
print("off30 独有:", sorted(set(c30)-set(c0))[:15])
print("off0 独有:", sorted(set(c0)-set(c30))[:15])
