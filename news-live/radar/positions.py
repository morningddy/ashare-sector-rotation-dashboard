# -*- coding: utf-8 -*-
"""持仓/自选股的本地持久化。

数据落盘到 data/positions.json，刷新页面不再丢失。

条目结构：{code, name, cost, shares}
  cost=0 或 shares=0 视为「自选股」——只跟踪行情/新闻/信号，不参与市值与盈亏统计。
"""
from __future__ import annotations

import json
from pathlib import Path

POS_FILE = Path(__file__).resolve().parent.parent / "data" / "positions.json"


def load_positions() -> list[dict]:
    """读取持仓，文件不存在或损坏时返回空列表。"""
    try:
        if POS_FILE.exists():
            data = json.loads(POS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                out = []
                for d in data:
                    if not isinstance(d, dict) or "code" not in d:
                        continue
                    out.append({
                        "code": str(d["code"]).zfill(6),
                        "name": d.get("name", str(d["code"])),
                        "cost": float(d.get("cost", 0) or 0),
                        "shares": int(d.get("shares", 0) or 0),
                    })
                return out
    except Exception:  # noqa: BLE001
        pass
    return []


def save_positions(positions: list[dict]) -> None:
    POS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POS_FILE.write_text(
        json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")


def is_watch(p: dict) -> bool:
    """是否自选股（未填成本/股数）。"""
    return not (p.get("cost") and p.get("shares"))


def parse_bulk(text: str, codelist: "object | None" = None) -> list[dict]:
    """批量解析输入，支持三种格式（每行一条）：

        000811,12.5,1000     代码,成本,股数
        000811 12.5 1000     空格分隔
        000811              仅代码 → 自选股
        冰轮环境            公司简称 → 自选股（需代码表）

    忽略 # 开头的注释行与空行。
    """
    import pandas as pd
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.replace(",", " ").replace("\t", " ").split()
                 if x.strip()]
        if not parts:
            continue
        head = parts[0]
        code = "".join(ch for ch in head if ch.isdigit())
        name = None
        if len(code) == 6:
            code = code.zfill(6)
        else:
            # 不是代码 → 当公司简称查代码表
            if isinstance(codelist, pd.DataFrame) and len(codelist):
                hit = codelist[codelist["name"] == head]
                if len(hit):
                    code = str(hit.iloc[0]["code"]).zfill(6)
                    name = head
                else:
                    continue
            else:
                continue
        cost = 0.0
        shares = 0
        if len(parts) >= 3:
            try:
                cost = float(parts[1])
                shares = int(float(parts[2]))
            except ValueError:
                cost = shares = 0
        elif len(parts) == 2:
            try:
                cost = float(parts[1])
            except ValueError:
                cost = 0.0
        if name is None and isinstance(codelist, pd.DataFrame) and len(codelist):
            hit = codelist[codelist["code"] == code]
            name = str(hit.iloc[0]["name"]) if len(hit) else code
        out.append({"code": code, "name": name or code,
                    "cost": cost, "shares": shares})
    return out
