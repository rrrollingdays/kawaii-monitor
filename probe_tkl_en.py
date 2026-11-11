#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四轮探测：首页 SOLD OUT 卡片结构 + 分类页发现"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def get(url):
    try:
        r = s.get(url, impersonate="chrome124", timeout=25)
        blocked = "Just a moment" in r.text[:8000]
        print(f"GET {url} -> {r.status_code} len={len(r.text)} blocked={blocked}")
        return r.text if (r.status_code == 200 and not blocked) else ""
    except Exception as e:
        print(f"GET {url} ERR {str(e)[:90]}")
        return ""

def clip(x, n=1000):
    return re.sub(r"\s+", " ", x)[:n]

home = get(BASE + "/")
if not home:
    raise SystemExit("home blocked")

# 1. 首页所有相对路径分组（修正版）
from urllib.parse import urlparse
paths = {}
for h in re.findall(r'href="([^"]+)"', home):
    p = urlparse(h if h.startswith("http") else "https://tokyokawaiilife.com/" + h.lstrip("/"))
    if p.netloc != "tokyokawaiilife.com":
        continue
    key = re.sub(r"\d+", "N", p.path.lstrip("/")) or "/"
    paths.setdefault(key, []).append(p.query or "")
print("[首页 path 模式]")
for k, v in sorted(paths.items()):
    print(f"  {k} x{len(v)}  query样例: {v[0][:80]}")

# 2. SOLD OUT 卡片完整结构（每个 SOLD OUT 位置的上下文）
idxs = [m.start() for m in re.finditer("SOLD OUT", home)]
print(f"\n[SOLD OUT 共 {len(idxs)} 处]")
i = idxs[0]
print("[第一个 SOLD OUT 上下文]\n", clip(home[max(0, i-2200):i+300], 3000))

# 3. SOLD OUT 与非 SOLD OUT 卡片对比：找第二个位置附近
if len(idxs) > 1:
    i2 = idxs[1]
    print("\n[第二个 SOLD OUT 上下文]\n", clip(home[max(0, i2-800):i2+200], 1200))

# 4. 有货卡片结构（取一个不出现在 SOLD OUT 附近的 goods 链接）
# 找出所有 goods 链接出现位置及是否 1500 字符内含 SOLD OUT
goods_pos = [m.start() for m in re.finditer(r'href="https://tokyokawaiilife\.com/goods_en_USD_(\d+)\.html"', home)]
in_stock = None
for gp in goods_pos:
    seg = home[gp:gp+2500]
    if "SOLD OUT" not in seg:
        in_stock = gp
        break
if in_stock:
    print("\n[有货卡片上下文]\n", clip(home[max(0, in_stock-300):in_stock+2200], 3000))
