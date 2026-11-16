# -*- coding: utf-8 -*-
"""轻量 A 股回测引擎 —— 参数化「纪律趋势」规则。

规则（源自 workbench 的冰轮环境策略）：
  1. 收盘站上 MA(fast) 且 MA(fast)>MA(slow) → 目标仓位 100%
  2. 跌破 MA(fast)                            → 目标仓位 50%
  3. 跌破 MA(slow)                            → 目标仓位 0%（清仓）
  4. 相对最近一次入场价亏损达 stop_loss        → 立刻清仓（铁律优先）

执行假设：信号次日开盘成交（T+1 近似）；开盘一字涨停不可买入、一字跌停不可卖出；
佣金双边万2.5（最低5元）、卖出印花税 0.05%。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COMMISSION_RATE = 2.5e-4   # 万2.5
MIN_COMMISSION = 5.0
STAMP_TAX = 5e-4           # 卖出印花税 0.05%


def run_backtest(df: pd.DataFrame, fast: int = 20, slow: int = 60,
                 stop_loss: float = 0.12, capital: float = 100_000.0,
                 benchmark: pd.DataFrame | None = None) -> dict:
    d = df.copy().reset_index(drop=True)
    d["ma_fast"] = d["close"].rolling(fast).mean()
    d["ma_slow"] = d["close"].rolling(slow).mean()

    def target_pos(row) -> int:
        if np.isnan(row["ma_fast"]) or np.isnan(row["ma_slow"]):
            return 0
        if row["close"] > row["ma_fast"] and row["ma_fast"] > row["ma_slow"]:
            return 100
        if row["close"] < row["ma_slow"]:
            return 0
        return 50

    d["target"] = d.apply(target_pos, axis=1)

    cash, shares = capital, 0
    entry_price = np.nan
    equity, trades = [], []
    pending = None  # (target_pct, 原因) 次日开盘执行

    for i in range(len(d)):
        row = d.iloc[i]
        prev = d.iloc[i - 1] if i else None
        open_gap = (row["open"] / prev["close"] - 1) if prev is not None and prev["close"] else 0

        # ---- 先执行昨日挂单（次日开盘成交）----
        if pending is not None:
            tgt, reason = pending
            pending = None
            limit_up = open_gap >= 0.098     # 一字涨停买不进
            limit_dn = open_gap <= -0.098    # 一字跌停卖不出
            if tgt == 0 and shares and not limit_dn:
                proceeds = _sell_all(row, shares, cash)
                trades.append(dict(日期=row["date"], 动作="清仓·" + reason,
                                   价格=row["open"], 股数=shares,
                                   盈亏=round(proceeds - entry_price * shares, 2)))
                cash, shares, entry_price = cash + proceeds, 0, np.nan
            elif tgt in (50, 100) and shares == 0 and not limit_up:
                shares, cash, entry_price = _buy(row, tgt, cash, capital)
                trades.append(dict(日期=row["date"], 动作=f"建仓{tgt}%", 价格=row["open"],
                                   股数=shares, 盈亏=0.0))
            elif tgt == 50 and shares and tgt == 100:
                pass  # 50→100 不加仓，保持简单

        # ---- 当日收盘评估止损（铁律，次日开盘执行）----
        if shares and not np.isnan(entry_price) and \
                row["close"] <= entry_price * (1 - stop_loss):
            pending = (0, f"硬止损 {stop_loss:.0%}")
        else:
            t_now = int(row["target"])
            if shares == 0 and t_now in (50, 100):
                pending = (t_now, "趋势成立")
            elif shares and t_now == 0:
                pending = (0, "跌破慢线")
            elif shares and t_now == 50 and pending is None and \
                    d.iloc[:i + 1]["target"].iloc[-2:].min() == 100 and t_now == 50:
                pass  # 100→50 不主动减半（保留仓位的纪律简化）

        equity.append(cash + shares * row["close"])

    equity = pd.Series(equity, index=d["date"], name="strategy")
    trades_df = pd.DataFrame(trades)
    metrics = _metrics(equity, capital)
    if benchmark is not None and len(benchmark):
        bench = benchmark.set_index("date")["close"].reindex(equity.index).ffill()
        bench = bench / bench.iloc[0] * capital
        metrics["基准收益%"] = round((bench.iloc[-1] / capital - 1) * 100, 2)
        equity = pd.concat([equity, bench.rename("benchmark")], axis=1)
    return {"equity": equity, "trades": trades_df, "metrics": metrics,
            "bars": len(d), "detail": d}


def _buy(row, tgt_pct: int, cash: float, base: float) -> tuple[int, float, float]:
    budget = cash if tgt_pct == 100 else base * 0.5
    price = row["open"]
    shares = int(budget // (price * 100)) * 100  # 整手
    if shares <= 0:
        return 0, cash, np.nan
    cost = shares * price
    fee = max(cost * COMMISSION_RATE, MIN_COMMISSION)
    return shares, cash - cost - fee, price


def _sell_all(row, shares: int, _cash: float) -> float:
    proceeds = shares * row["open"]
    fee = max(proceeds * COMMISSION_RATE, MIN_COMMISSION) + proceeds * STAMP_TAX
    return proceeds - fee


def _metrics(equity: pd.Series, capital: float) -> dict:
    ret = equity.iloc[-1] / capital - 1
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    annual = (1 + ret) ** (365 / days) - 1
    roll_max = equity.cummax()
    dd = (equity / roll_max - 1).min()
    daily = equity.pct_change().dropna()
    sharpe = float(daily.mean() / (daily.std() + 1e-12) * np.sqrt(252)) if len(daily) > 2 else 0.0
    return {
        "总收益%": round(ret * 100, 2),
        "年化%": round(annual * 100, 2),
        "最大回撤%": round(dd * 100, 2),
        "夏普": round(sharpe, 2),
        "交易次数": None,  # 由调用方补充
    }
