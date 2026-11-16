# -*- coding: utf-8 -*-
"""Wind Alice 增强层：经 wind-mcp-skill 的本地 CLI 调 Wind MCP 取数。

状态判定（三者齐备才真正发起调用，否则上层显示「Wind 未接入」并走免费源）：
  installed —— ~/.agents/skills/wind-mcp-skill/scripts/cli.mjs 存在
  has_key   —— ~/.wind-aifinmarket/config 内含 WIND_API_KEY
  node_ok   —— 能调起 node（CLI 是 .mjs）

⚠️ 计费与调用纪律（来自 wind-mcp-skill 官方 SKILL.md，必须遵守）：
  1. **每次调用消耗账户额度**，所以 UI 里一律做成「手动按钮触发」，
     绝不能放进页面自动刷新路径（否则一次刷新就烧掉几十次调用）。
  2. **批量必须串行 + 先探针**：≥2 个标的时先只发第一个，成功才继续，
     失败立即终止整批，不要把同一个错误调用扩散到其它标的。
  3. 失败信封形如 {"ok": false, "code": "AUTH_ERROR"|"RATE_LIMIT_ERROR"|...,
     "message": "..."}；成功时业务数据在 content[0].text（多为 JSON 字符串）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pandas as pd

HOME = Path.home()
SKILL_DIR = HOME / ".agents" / "skills" / "wind-mcp-skill"
CLI = SKILL_DIR / "scripts" / "cli.mjs"
WIND_CONFIG = HOME / ".wind-aifinmarket" / "config"

# server_type -> 可用工具（与 scripts/tool-manifest.json 一致）
SERVERS = {
    "stock_data": ["get_stock_quote", "get_stock_kline", "get_stock_basicinfo",
                   "get_stock_fundamentals", "get_stock_technicals",
                   "get_stock_price_indicators", "get_risk_metrics",
                   "get_stock_events", "get_stock_equity_holders",
                   "search_stocks"],
    "index_data": ["get_index_quote", "get_index_kline", "get_index_basicinfo",
                   "get_index_fundamentals", "get_index_technicals",
                   "get_index_price_indicators"],
    "fund_data": ["get_fund_quote", "get_fund_info", "get_fund_holdings",
                  "get_fund_performance", "search_funds"],
    "financial_docs": ["get_company_announcements", "get_financial_news"],
    "economic_data": ["search_economic_indicator",
                      "query_economic_indicator_data"],
    "bond_data": ["get_bond_basicinfo", "get_bond_market_data"],
    "analytics_data": ["get_financial_data"],
}

# 轻量内存缓存：同一会话内重复看同一只票不重复烧额度
_CACHE: dict[tuple, tuple[float, object]] = {}
_CACHE_TTL = 900  # 15 分钟


def wind_status() -> dict:
    """检测本机 Wind 接入状态。不消耗额度。"""
    has_key = False
    if WIND_CONFIG.exists():
        try:
            for line in WIND_CONFIG.read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s.startswith("WIND_API_KEY=") and len(s.split("=", 1)[1].strip()) > 8:
                    has_key = True
                    break
        except Exception:  # noqa: BLE001
            pass

    node = shutil.which("node")
    if node is None:
        # 托管 node 不在 PATH 里时兜底找一下
        for p in sorted(HOME.glob(".workbuddy/binaries/node/versions/*/node.exe")):
            node = str(p)
            break

    installed = CLI.exists()
    return {
        "installed": installed,
        "cli_path": str(CLI),
        "has_key": has_key,
        "node": node or "",
        "node_ok": bool(node),
        "ready": installed and has_key and bool(node),
        "config_path": str(WIND_CONFIG),
        "key_url": "https://market.windalice.com/#/user/overview",
    }


def _node_cmd() -> list[str]:
    st = wind_status()
    return [st["node"] or "node", str(CLI)]


def wind_call(tool: str, params: dict, server: str = "stock_data",
              timeout: int = 30, use_cache: bool = True) -> dict:
    """调一次 Wind MCP 工具，返回规范信封（永不抛异常）。

    返回: {"ok": bool, "code": str, "message": str, "payload": object}
      ok=True  → payload 是解析后的业务数据（dict / list / str）
      ok=False → code ∈ AUTH_ERROR / RATE_LIMIT_ERROR / NETWORK_ERROR /
                 EMPTY / PARSE_ERROR / NOT_READY / TIMEOUT
    """
    st = wind_status()
    if not st["ready"]:
        return {"ok": False, "code": "NOT_READY", "message": "Wind 未接入",
                "payload": None, "status": st}

    key = (server, tool, json.dumps(params, ensure_ascii=False, sort_keys=True))
    if use_cache:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]

    cmd = _node_cmd() + ["call", server, tool,
                         json.dumps(params, ensure_ascii=False)]
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, shell=False,
                              cwd=str(SKILL_DIR))
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "TIMEOUT",
                "message": f"Wind 调用超时（>{timeout}s）", "payload": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "NETWORK_ERROR", "message": str(e),
                "payload": None}

    out = (proc.stdout or "").strip()
    if not out:
        msg = (proc.stderr or "").strip() or "CLI 无输出"
        return {"ok": False, "code": "EMPTY", "message": msg[:300],
                "payload": None}

    try:
        env_ = json.loads(out)
    except Exception:  # noqa: BLE001
        # 不是 JSON 就把原文当 payload（有些工具直接吐文本）
        res = {"ok": True, "code": "OK", "message": "", "payload": out}
        if use_cache:
            _CACHE[key] = (time.time(), res)
        return res

    # 失败信封：{"ok": false, "code": "...", "message": "..."}
    if isinstance(env_, dict) and env_.get("ok") is False:
        res = {"ok": False, "code": str(env_.get("code") or "ERROR"),
               "message": str(env_.get("message") or "")[:300],
               "payload": None}
        # 失败不缓存，避免把 AUTH_ERROR 钉死 15 分钟
        return res

    payload = _unwrap(env_)
    res = {"ok": True, "code": "OK", "message": "", "payload": payload}
    if use_cache:
        _CACHE[key] = (time.time(), res)
    return res


def _unwrap(env_) -> object:
    """从 CLI 信封里剥出业务数据。

    成功形状（实测）：
        {"content": [{"type": "text", "text": "<JSON 字符串>"}], "cli_meta": {...}}
    业务 JSON 形状：
        {"data": {"data": [{"columns": [{"name": ...}], "rows": [[...]]}]},
         "error": null}
    """
    if not isinstance(env_, dict):
        return env_

    text = None
    for it in (env_.get("content") or []):
        if isinstance(it, dict) and it.get("type") == "text" \
                and isinstance(it.get("text"), str):
            text = it["text"]
            break
    if text is None:
        # 没有 content 就直接看有没有 data
        return env_.get("data", env_)

    try:
        inner = json.loads(text)
    except Exception:  # noqa: BLE001
        return text

    # 业务错误可能包在 content[0].text 的 JSON 里
    if isinstance(inner, dict):
        if inner.get("error"):
            return {"__error__": inner["error"]}
        if isinstance(inner.get("data"), dict) and inner["data"].get("error"):
            return {"__error__": inner["data"]["error"]}
    return inner.get("data", inner) if isinstance(inner, dict) else inner


def wind_table(env: dict) -> tuple[pd.DataFrame | None, str]:
    """把 wind_call 信封转成 DataFrame。

    Wind 的表格形状是 {"data": [{"columns": [{"name":...}], "rows": [[...]]}]}
    （可能有多块表，这里取第一块；多块用 wind_tables）。
    """
    if not env.get("ok"):
        return None, f"{env.get('code')}: {env.get('message')}"
    p = env.get("payload")
    if isinstance(p, dict) and "__error__" in p:
        return None, str(p["__error__"])[:300]
    blocks = _blocks(p)
    if not blocks:
        return None, "Wind 返回空数据"
    blk = blocks[0]
    cols = [c.get("name") for c in (blk.get("columns") or [])]
    rows = blk.get("rows") or []
    if not cols or not rows:
        return None, "Wind 返回空数据"
    return pd.DataFrame(rows, columns=cols), ""


def wind_tables(env: dict) -> list[pd.DataFrame]:
    """返回 Wind 结果里的全部表格块。"""
    if not env.get("ok"):
        return []
    return [pd.DataFrame(b.get("rows") or [],
                         columns=[c.get("name") for c in (b.get("columns") or [])])
            for b in _blocks(env.get("payload"))
            if b.get("columns") and b.get("rows")]


def _blocks(payload) -> list[dict]:
    """从各种嵌套形状里挖出 [{columns, rows}]。"""
    if payload is None:
        return []
    if isinstance(payload, dict):
        if "columns" in payload:
            return [payload]
        for k in ("data", "result", "content"):
            sub = _blocks(payload.get(k))
            if sub:
                return sub
        return []
    if isinstance(payload, list):
        out: list[dict] = []
        for it in payload:
            out.extend(_blocks(it))
        return out
    return []


# ---------------- 面向业务的封装（每个只花 1 次调用）----------------

def _code6(code: str) -> str:
    """600519 → 600519.SH；000811 → 000811.SZ；8/4 开头 → .BJ"""
    s = str(code).strip().upper()
    if "." in s:
        return s
    d = s[:6].zfill(6)
    if d[0] in ("6", "9") or d.startswith("11") or d.startswith("78"):
        return d + ".SH"
    if d[0] in ("0", "2", "3"):
        return d + ".SZ"
    if d[0] in ("4", "8"):
        return d + ".BJ"
    return d + ".SH"


def wind_profile(code: str) -> tuple[dict, str]:
    """公司档案：Wind 行业分类 / 概念板块 / 主营构成（免费源拿不到的）。"""
    wc = _code6(code)
    env = wind_call("get_stock_basicinfo", {"question": f"查询 {wc} 的简称、上市板、"
                                            "上市日期、WIND行业分类、所属概念板块、主营产品"})
    df, err = wind_table(env)
    if df is None:
        return {}, err
    row = df.iloc[0].to_dict()
    return {str(k): ("" if v is None else v) for k, v in row.items()}, ""


def wind_technicals(code: str) -> tuple[pd.DataFrame | None, str]:
    """技术指标（MACD/RSI/均线等）。"""
    env = wind_call("get_stock_technicals", {"windcode": _code6(code)})
    return wind_table(env)


def wind_risk(code: str) -> tuple[pd.DataFrame | None, str]:
    """风险指标（波动率/夏普/最大回撤等）。"""
    env = wind_call("get_risk_metrics", {"windcode": _code6(code)})
    return wind_table(env)


def wind_fundamentals(code: str) -> tuple[pd.DataFrame | None, str]:
    """主要财务指标。"""
    env = wind_call("get_stock_fundamentals", {"windcode": _code6(code)})
    return wind_table(env)


def wind_holders(code: str) -> tuple[pd.DataFrame | None, str]:
    """股东结构。"""
    env = wind_call("get_stock_equity_holders", {"windcode": _code6(code)})
    return wind_table(env)


def wind_events(code: str) -> tuple[pd.DataFrame | None, str]:
    """公司事件（分红/增发/解禁/诉讼等）。"""
    env = wind_call("get_stock_events", {"windcode": _code6(code)})
    return wind_table(env)


def wind_announcements(code: str, limit: int = 20) -> tuple[pd.DataFrame | None, str]:
    """公司公告。"""
    env = wind_call("get_company_announcements",
                    {"windcode": _code6(code), "limit": limit})
    return wind_table(env)


def wind_news(code: str, limit: int = 20) -> tuple[pd.DataFrame | None, str]:
    """Wind 财经新闻（比免费电报流覆盖广）。"""
    env = wind_call("get_financial_news",
                    {"windcode": _code6(code), "limit": limit})
    return wind_table(env)
