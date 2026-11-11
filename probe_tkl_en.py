#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三轮探测：列表页 / sitemap / 首页卡片结构"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def get(url, quiet=False):
    try:
        r = s.get(url, impersonate="chrome124", timeout=25)
        blocked = "Just a moment" in r.text[:8000] or "Attention Required" in r.text[:8000]
        if not quiet:
            print(f"GET {url} -> {r.status_code} len={len(r.text)} blocked={blocked}")
        return (r.status_code, r.text) if (r.status_code == 200 and not blocked) else (r.status_code, "")
    except Exception as e:
        print(f"GET {url} ERR {str(e)[:90]}")
        return 0, ""

def clip(x, n=800):
    return re.sub(r"\s+", " ", x)[:n]

# 1. robots / sitemap
get(BASE + "/robots.txt")
st, sm = get(BASE + "/sitemap.xml", quiet=True)
if sm:
    print("[sitemap 前2条]", clip(sm, 500))
    print("[sitemap 含goods数]", len(re.findall(r"goods_", sm)))
    print("[sitemap 其他URL型]", sorted(set(re.findall(r"<loc>(https://[^<]*?)</loc>", sm)))[:20])

# 2. 首页全链接分组
st, home = get(BASE + "/", quiet=True)
links = re.findall(r'href="([^"]+)"', home)
groups = {}
for h in links:
    m = re.search(r"/([^/?#]+)", h if h.startswith("http") else "https://x.com/" + h.lstrip("/"))
    if m:
        p = m.group(1)
        key = re.sub(r"[\d]+", "N", p)
        groups.setdefault(key, []).append(h)
print("\n[首页链接模式汇总]")
for k, v in sorted(groups.items()):
    print(f"  {k} x{len(v)}  e.g. {v[0][:110]}")

# 3. 首页商品卡片完整片段（看有无 SOLD OUT/完売 标记、价格结构）
i = home.find("goods_en_USD_")
if i > 0:
    print("\n[首页商品卡片片段]\n", clip(home[max(0,i-1500):i+1500], 2500))
print("\n[首页 SOLDOUT/完売/Sold 关键词]", {kw: home.count(kw) for kw in ["SOLD", "Sold", "sold", "完売", "OUT"]})

# 4. 商品页多语言/货币变体
for variant in ["goods_en_JPY_2684.html", "goods_ja_JPY_2684.html", "goods_zhcn_JPY_2684.html", "goods_en_USD_2684.html?x=1"]:
    st2, body = get(f"{BASE}/{variant}", quiet=True)
    print(f"  variant {variant} -> {st2} {'BLOCKED' if not body else 'OK'}")

# 5. content.php 试探（若首页存在）
m = re.search(r'href="([^"]*content[^"]*)"', home)
if m:
    st3, cbody = get(m.group(1) if m.group(1).startswith("http") else BASE + "/" + m.group(1).lstrip("/"))
    if cbody:
        print("[content页 goods链接数]", len(re.findall(r"goods_", cbody)))
