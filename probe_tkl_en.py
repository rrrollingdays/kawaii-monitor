#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第六轮探测：分类页列表区卡片原始结构 + 分页/件数"""
import re
from curl_cffi import requests

BASE = "https://tokyokawaiilife.com"
s = requests.Session()

def clip(x, n=1600):
    return re.sub(r"\s+", " ", x)[:n]

body = s.get(f"{BASE}/index.php?genre_id=17&offset=0&lang_id=en&tsuka_conv=USD", impersonate="chrome124", timeout=25).text
print("len:", len(body), "blocked:", "Just a moment" in body[:8000])

# 1. 件数/结果数文本
for m in re.finditer(r'[\d,\s]+\s*件', body):
    seg = body[max(0, m.start()-120):m.end()+30]
    if "goods" in seg or "item" in seg.lower() or "result" in seg.lower() or re.search(r'>(\s*[\d,]+\s*件)', seg):
        print("[件数文本]", clip(seg, 200))

# 2. 列表区第一个卡片完整结构（从"商品一覧"注释开始）
i = body.find("商品一覧")
print("\n[商品一覧注释位置]", i)
if i > 0:
    print("[列表区开头 1600 字符]\n", clip(body[i:i+1600], 1700))

# 3. 所有 li 开头样式统计（列表区）
li_forms = re.findall(r'<li[^>]*>\s*<a[^>]*goods_en_USD_\d+\.html', body)
print("\n[li→goods 形态统计]", {f[:60]: li_forms.count(f) for f in set(li_forms)})

# 4. 全部 goods ID 数（去重）
ids = sorted(set(re.findall(r'goods_en_USD_(\d+)\.html', body)))
print("[本页 goods ID 总数]", len(ids), ids[:20])

# 5. offset=30 页是否有效
body2 = s.get(f"{BASE}/index.php?genre_id=17&offset=30&lang_id=en&tsuka_conv=USD", impersonate="chrome124", timeout=25).text
ids2 = sorted(set(re.findall(r'goods_en_USD_(\d+)\.html', body2)))
print("[offset=30 页 ID 数]", len(ids2), "与第0页交集:", len(set(ids) & set(ids2)))

# 6. genre=8 (ALL) 的页数情况
ids8 = []
for off in [0, 30, 60, 90]:
    b = s.get(f"{BASE}/index.php?genre_id=8&offset={off}&lang_id=en&tsuka_conv=USD", impersonate="chrome124", timeout=25).text
    cur = set(re.findall(r'goods_en_USD_(\d+)\.html', b))
    ids8.extend(cur)
    print(f"[genre8 offset={off}] 唯一ID {len(cur)}")
print("[genre8 全部去重]", len(set(ids8)))
