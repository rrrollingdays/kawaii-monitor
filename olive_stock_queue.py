"""Export confirmed inventory transitions for the OLIVE pilot."""

import csv
import hashlib
import os
import re
import unicodedata
from pathlib import Path

ITEM_CODE = "OLV1062501A0001"
TAOBAO_ITEM_ID = "1100552597245"
COLORS = {"30161128": "黑色", "30161126": "象牙白", "30161127": "棕色"}
COUNT = re.compile(r"(?:残り|残\s*:|あと)\s*([0-9]+)\s*(?:点|個|件|枚|着)")
LOW = re.compile(r"(?:残り?|在庫)\s*(?:わずか|僅か|少なめ|少量|少(?=\s|$))")


def target_quantity(event):
    if event["type"] == "SOLD_OUT":
        return 0
    hint = unicodedata.normalize("NFKC", event["stock_hint"])
    count = COUNT.search(hint)
    if count:
        return int(count.group(1))
    return 2 if LOW.search(hint) else 5


def export_stock_updates(events, destination):
    rows = []
    for event in events:
        if event["type"] not in {"SOLD_OUT", "RESTOCK"} or event["number"] != ITEM_CODE:
            continue
        source_sku = event.get("source_sku_id")
        color = COLORS.get(source_sku)
        if not color:
            continue
        observed = event["time"]
        key = f"{ITEM_CODE}:{source_sku}:{event['type']}:{observed}"
        rows.append((
            hashlib.sha256(key.encode()).hexdigest(),
            observed, ITEM_CODE, TAOBAO_ITEM_ID, source_sku,
            f"{color}（预约，先下单后采购）", "均码",
            "卖空" if event["type"] == "SOLD_OUT" else "补货",
            target_quantity(event), "待处理",
        ))
    if not rows:
        return 0
    path = Path(destination)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow((
            "事件编号", "检测时间", "商家编码", "淘宝商品ID", "来源SKUID",
            "淘宝颜色分类", "淘宝尺码", "库存变化", "目标库存", "处理状态",
        ))
        writer.writerows(rows)
    os.replace(temporary, path)
    return len(rows)
