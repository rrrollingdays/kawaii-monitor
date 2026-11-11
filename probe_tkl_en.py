#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 tokyokawaiilife.com（LIZ 海外店，multilingualcart 平台）页面结构。
输出精简结构摘要到日志，用于编写正式监控脚本。"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
UA_HEADERS = {"Accept-Language": "ja,en-US;q=0.9,zhcn;q=0.8"}

def get(url):
    r = requests.get(url, impersonate="chrome124", timeout=25, headers=UA_HEADERS)
    body = r.text
    blocked = ("Just a moment" in body[:8000]) or ("Attention Required" in body[:8000])
    print(f"== GET {url} -> {r.status_code} len={len(body)} blocked={blocked}")
    return body

def clip(s, n=600):
    return re.sub(r"\s+", " ", s)[:n]

# ---------- 1. 首页：找链接模式 ----------
home = get(BASE + "/")
hrefs = re.findall(r'href="([^"]+)"', home)
goods_links = [h for h in hrefs if "goods" in h]
list_links = [h for h in hrefs if re.search(r"(list|cate|search)", h, re.I)]
print("\n[首页 goods 链接样例]")
for h in list(dict.fromkeys(goods_links))[:10]:
    print("  ", h)
print("[首页 list/cate 链接样例]")
for h in list(dict.fromkeys(list_links))[:15]:
    print("  ", h)

# 首页上一个商品卡片的原始片段
m = re.search(r'<[^>]+>.{0,200}goods_zhcn[^"]*\.html.{0,800}', home, re.S)
if m:
    print("\n[首页商品卡片片段]\n", clip(m.group(0), 1200))

# ---------- 2. 列表页：抓第一个分类/列表链接 ----------
list_url = None
for h in list_links:
    full = h if h.startswith("http") else BASE + "/" + h.lstrip("./")
    if re.search(r"(list|cate|search)", h, re.I) and ".php" in h or "list" in h:
        list_url = full
        break
if list_url:
    lst = get(list_url)
    gl = list(dict.fromkeys(re.findall(r'href="([^"]*goods[^"]*\.html[^"]*)"', lst)))[:10]
    print("\n[列表页 goods 链接样例]", list_url)
    for h in gl:
        print("  ", h)
    print("[列表页翻页控件]\n", clip(re.sub(r"<[^>]+>", "|", lst)[-3000:], 600))
    m = re.search(r'<[^>]+>.{0,300}goods_zhcn[^"]*\.html.{0,1000}', lst, re.S)
    if m:
        print("\n[列表页商品卡片片段]\n", clip(m.group(0), 1500))

# ---------- 3. 商品页 ----------
p = get(BASE + "/goods_zhcn_USD_2684.html")
print("\n[商品页 title]", re.search(r"<title>(.*?)</title>", p, re.S).group(1) if re.search(r"<title>(.*?)</title>", p, re.S) else "?")
for kw in ["SOLD", "sold", "在庫", "stock", "cart", "Cart", "数量"]:
    idxs = [m.start() for m in re.finditer(kw, p)][:3]
    for i in idxs:
        print(f"\n[商品页 '{kw}' 上下文 @{i}]\n", clip(p[max(0, i-250):i+350], 700))
# 选项/下拉框
for m in list(re.finditer(r"<select[^>]*>.*?</select>", p, re.S))[:3]:
    print("\n[商品页 select]\n", clip(m.group(0), 800))
# 价格区域
for m in list(re.finditer(r"(USD|JPY|\\$|¥).{0,300}", p))[:5]:
    print("\n[商品页价格片段]\n", clip(m.group(0), 350))
