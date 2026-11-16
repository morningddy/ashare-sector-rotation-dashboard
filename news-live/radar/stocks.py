# -*- coding: utf-8 -*-
"""行情与数据封装（多源，全免费）。

## 数据源优先级（实测结论，见 README「数据源容错设计」）

| 数据 | 首选 | 兜底 1 | 兜底 2 |
|---|---|---|---|
| 实时行情 | 腾讯 qt.gtimg.cn (0.1s) | 东财 push2 | 新浪 hq.sinajs.cn |
| 日线 | 腾讯 ifzq.gtimg.cn (0.2s) | 东财 push2his | 新浪 / akshare |
| 全市场快照 | 东财 spot_em | 新浪 spot | — |
| 资金流 | 东财 push2 fflow | akshare 同花顺 | — |
| 公司档案 | 东财 push2 stock/get | akshare F10 | — |

⚠️ **关键坑**：东财 `push2.eastmoney.com` 在部分网络环境**整段不可达**
（`ConnectionError: RemoteDisconnected`），且失败是静默的（akshare 会吞异常
返回空 DataFrame）。而腾讯/新浪源只需 0.1~0.2s 且极少失败。
因此**主源一律用腾讯**，东财只作为兜底。
"""
from __future__ import annotations

import functools
import json
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

# 东财接口请求头（push2 对 UA/Referer 敏感）
_EM_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/",
               "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
# 腾讯行情请求头
_TX_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}
# 新浪行情请求头（hq.sinajs.cn 强制校验 Referer）
_SINA_HEADERS = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}


# ---------------- 基础工具 ----------------

def market_of(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("6", "9", "5")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    return "bj"


def em_secid(code: str) -> str:
    """东财 secid：沪市 1.xxxxxx，深市 0.xxxxxx。"""
    code = str(code).zfill(6)
    return f"{'1' if market_of(code) == 'sh' else '0'}.{code}"


def _http_get(url: str, headers: dict | None = None, timeout: int = 8,
              attempts: int = 2) -> str | None:
    """通用 GET，返回文本；失败返回 None（不抛异常）。带轻量重试。

    ⚠️ 显式传 `proxies={}` 直连。Windows 注册表里常残留失效代理，
    requests 会自动拾取（表现为 ConnectionError / ProxyError），
    仅靠 NO_PROXY 环境变量不可靠（`NO_PROXY="*"` 还会破坏 getproxies() 结果）。
    """
    import time
    import requests
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers or _EM_HEADERS,
                             timeout=timeout, proxies={})
            if r.status_code == 200:
                return r.text
        except Exception:  # noqa: BLE001
            pass
        if i < attempts - 1:
            time.sleep(0.4 * (i + 1))
    return None


def _retry(fn, attempts: int = 3, delay: float = 1.5):
    """简单重试：东财接口存在间歇性 RemoteDisconnected。"""
    import time
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last  # type: ignore[misc]


# ---------------- 腾讯源（主源，最快最稳） ----------------

def _tx_symbol(code: str) -> str:
    return f"{market_of(code)}{code}"


def tx_realtime(code: str) -> dict | None:
    """腾讯实时行情（qt.gtimg.cn）。

    返回 `v_sh601899="1~紫金矿业~601899~30.01~31.41~..."`，`~` 分隔：
    1 名称 / 3 现价 / 4 昨收 / 5 今开 / 6 成交量(手) / 31 涨跌 / 32 涨跌幅
    / 33 最高 / 34 最低 / 37 成交额(万) / 38 换手率 / 39 市盈率
    """
    txt = _http_get(f"https://qt.gtimg.cn/q={_tx_symbol(code)}", _TX_HEADERS)
    if not txt or '="' not in txt:
        return None
    try:
        p = txt.split('="', 1)[1].rstrip('";\n ').split("~")
        if len(p) < 40:
            return None

        def f(i, d=0.0):
            try:
                return float(p[i])
            except (ValueError, IndexError):
                return d

        return {"代码": code, "名称": p[1], "最新价": f(3), "昨收": f(4),
                "今开": f(5), "涨跌": f(31), "涨跌幅": f(32),
                "最高": f(33), "最低": f(34), "成交额": f(37) * 1e4,
                "换手率": f(38), "市盈率": f(39)}
    except Exception:  # noqa: BLE001
        return None


def tx_daily(code: str, start: str = "20240101", end: str = "20991231",
             adjust: str = "qfq") -> pd.DataFrame | None:
    """腾讯日线（web.ifzq.gtimg.cn），列名与 daily_history 输出一致。

    返回行结构：[日期, 开, 收, 高, 低, 成交量(手), 成交额?]
    注意：部分标的只返回 **6 列**（无成交额），必须容错。
    """
    fq = adjust if adjust in ("qfq", "hfq") else ""
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}" if len(start) == 8 else start
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}" if len(end) == 8 else end
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={_tx_symbol(code)},day,{s},{e},1300,{fq}")
    txt = _http_get(url, _TX_HEADERS, timeout=12)
    if not txt:
        return None
    try:
        node = json.loads(txt)["data"][_tx_symbol(code)]
    except Exception:  # noqa: BLE001
        return None
    rows = node.get(f"{fq}day") or node.get("day") or []
    if not rows:
        return None

    def num(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    recs = []
    for r in rows:
        if len(r) < 6:
            continue
        vol = num(r[5]) * 100            # 手 → 股
        amt = num(r[6]) if len(r) > 6 and r[6] not in (None, "") else 0.0
        recs.append({"date": r[0], "open": num(r[1]), "close": num(r[2]),
                     "high": num(r[3]), "low": num(r[4]),
                     "volume": vol, "amount": amt})
    if not recs:
        return None
    df = pd.DataFrame(recs)
    # 成交额缺失时用 均价×量 估算，保证 amount 列存在（回测/展示都依赖）
    if (df["amount"] == 0).all():
        df["amount"] = (df["volume"] * (df["open"] + df["close"]) / 2).round(0)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ---------------- 东财源（兜底） ----------------

def _em_get(url: str, params: dict | None = None, timeout: int = 8,
            attempts: int = 2) -> dict | None:
    """直连东财 JSON 接口。⚠️ push2 域名在部分网络整段不可达，调用方需备兜底。

    显式 `proxies={}` 绕过注册表残留代理（见 `_http_get` 说明）。
    """
    import time
    import requests
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=_EM_HEADERS,
                             timeout=timeout, proxies={})
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        if i < attempts - 1:
            time.sleep(0.5)
    return None


# ---------------- 对外接口（多源编排） ----------------

def daily_history(code: str, start: str = "20240101", end: str = "20991231",
                  adjust: str = "qfq") -> pd.DataFrame:
    """个股日线。返回列：date,open,close,high,low,volume,amount

    链：① 腾讯 ifzq（主，0.2s）→ ② 东财 push2his → ③ akshare（东财/新浪封装）
    ETF/场内基金（5/1 开头）腾讯 secid 规则不同，直接走 akshare。
    """
    code = str(code).zfill(6)
    is_fund = code.startswith(("5", "1"))

    # ① 腾讯
    if not is_fund:
        df = tx_daily(code, start, end, adjust)
        if df is not None and len(df):
            return df

    # ② 东财 push2his 直连
    if not is_fund:
        fq = {"qfq": 1, "hfq": 2, "": 0}.get(adjust, 1)
        js = _em_get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                     {"secid": em_secid(code), "klt": 101, "fqt": fq,
                      "beg": start, "end": end, "lmt": 10000,
                      "fields1": "f1,f2,f3,f4,f5,f6",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"})
        if js and js.get("data") and js["data"].get("klines"):
            rows = []
            for line in js["data"]["klines"]:
                p = line.split(",")
                rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                             "high": float(p[3]), "low": float(p[4]),
                             "volume": float(p[5] or 0), "amount": float(p[6] or 0)})
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)

    # ③ akshare
    import akshare as ak
    try:
        if is_fund:  # 场内基金/ETF
            df = _retry(lambda: ak.fund_etf_hist_em(
                symbol=code, period="daily", start_date=start,
                end_date=end, adjust=adjust))
        else:
            df = _retry(lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start,
                end_date=end, adjust=adjust))
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low",
                                "成交量": "volume", "成交额": "amount"})
    except Exception:
        df = _retry(lambda: ak.stock_zh_a_daily(
            symbol=_tx_symbol(code), adjust=adjust,
            start_date=start, end_date=end))
        df = df.rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _sina_symbol(symbol: str) -> str:
    """指数代码 → 新浪符号：399 开头走 sz，其余默认 sh。"""
    s = str(symbol).zfill(6)
    return ("sz" if s.startswith("399") else "sh") + s


def index_history(symbol: str = "000300", start: str = "20240101",
                  end: str = "20991231") -> pd.DataFrame:
    """指数日线，用于回测基准。

    链：① 腾讯 ifzq（主）→ ② 东财 index_zh_a_hist → ③ 新浪 stock_zh_index_daily
    （后两者取全量后本地裁剪日期）
    """
    sym = str(symbol).zfill(6)
    # ① 腾讯：指数代码前缀规则特殊，000300 走 sh、399xxx 走 sz
    tx_code = ("sz" if sym.startswith("399") else "sh") + sym
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}" if len(start) == 8 else start
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}" if len(end) == 8 else end
    txt = _http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    f"?param={tx_code},day,{s},{e},1300,qfq", _TX_HEADERS, timeout=12)
    if txt:
        try:
            node = json.loads(txt)["data"][tx_code]
            rows = node.get("qfqday") or node.get("day") or []
            if rows:
                df = pd.DataFrame([{
                    "date": r[0], "open": float(r[1]), "close": float(r[2]),
                    "high": float(r[3]), "low": float(r[4]),
                    "volume": float(r[5] or 0) * 100,
                } for r in rows])
                df["date"] = pd.to_datetime(df["date"])
                return df.sort_values("date").reset_index(drop=True)
        except Exception:  # noqa: BLE001
            pass

    # ② / ③ akshare
    import akshare as ak
    ren = {"日期": "date", "开盘": "open", "收盘": "close",
           "最高": "high", "最低": "low"}
    try:
        df = ak.index_zh_a_hist(symbol=sym, period="daily",
                                start_date=start, end_date=end)
        df = df.rename(columns=ren)
    except Exception:
        df = ak.stock_zh_index_daily(symbol=_sina_symbol(symbol))
        df = df.rename(columns=ren)  # 新浪已是英文列名，rename 幂等
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    sd, ed = pd.Timestamp(start), pd.Timestamp(end)
    return df[(df["date"] >= sd) & (df["date"] <= ed)].reset_index(drop=True)


@functools.lru_cache(maxsize=4)
def spot_snapshot() -> pd.DataFrame:
    """全市场实时快照（含涨跌幅/换手率）。

    链：东财 stock_zh_a_spot_em → 新浪 stock_zh_a_spot（慢，代码带 sh/sz 前缀）。
    失败时返回空 DataFrame 而非抛异常（调用方降级到单票接口）。
    """
    import akshare as ak
    try:
        return _retry(lambda: ak.stock_zh_a_spot_em(), attempts=2, delay=1.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        df = ak.stock_zh_a_spot()
        df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})")[0]
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["代码", "名称", "最新价", "涨跌幅", "今开",
                                     "最高", "最低", "成交额", "换手率"])


def realtime_row(code: str) -> dict | None:
    """单只股票实时行情（字段名统一）。

    链：① 腾讯 qt（主，0.1s）→ ② 快照缓存 → ③ 东财 push2 → ④ 新浪 hq。
    """
    code = str(code).zfill(6)

    # ① 腾讯
    r = tx_realtime(code)
    if r and r.get("最新价"):
        return {"代码": code, "名称": r["名称"], "最新价": r["最新价"],
                "涨跌幅": r["涨跌幅"], "今开": r["今开"], "最高": r["最高"],
                "最低": r["最低"], "昨收": r["昨收"], "成交额": r["成交额"],
                "换手率": r["换手率"]}

    # ② 快照
    try:
        df = spot_snapshot()
        if len(df):
            hit = df[df["代码"] == code]
            if len(hit):
                return hit.iloc[0].to_dict()
    except Exception:  # noqa: BLE001
        pass

    # ③ 东财
    js = _em_get("https://push2.eastmoney.com/api/qt/stock/get",
                 {"secid": em_secid(code),
                  "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170"})
    if js and js.get("data"):
        d = js["data"]
        sc = d.get("f59", 100) or 100

        def px(v):
            return round(v / (10 ** sc), sc) if isinstance(v, (int, float)) else "-"

        last, prev = d.get("f43"), d.get("f60")
        pct = ((last / prev - 1) * 100) if (last and prev) else 0.0
        return {"代码": code, "名称": d.get("f58", ""), "最新价": px(last),
                "涨跌幅": round(pct, 2), "今开": px(d.get("f46")),
                "最高": px(d.get("f44")), "最低": px(d.get("f45")),
                "昨收": px(prev), "成交额": d.get("f48", 0),
                "换手率": (d.get("f168") or 0) / 100}

    # ④ 新浪实时（hq.sinajs.cn，须带 Referer）
    txt = _http_get(f"https://hq.sinajs.cn/list={_tx_symbol(code)}", _SINA_HEADERS)
    if txt and '="' in txt:
        try:
            p = txt.split('="', 1)[1].rstrip('";\n ').split(",")
            name, opn, prev, last = p[0], float(p[1]), float(p[2]), float(p[3])
            pct = ((last / prev - 1) * 100) if prev else 0.0
            return {"代码": code, "名称": name, "最新价": last, "涨跌幅": round(pct, 2),
                    "今开": opn, "最高": float(p[4]), "最低": float(p[5]),
                    "昨收": prev, "成交额": float(p[9]), "换手率": 0}
        except Exception:  # noqa: BLE001
            pass
    return None


def stock_basic_info(code: str) -> dict:
    """个股档案（F10 摘要）：市值/PE/PB/行业。

    链：① 东财 push2 直连 → ② akshare F10 → ③ 腾讯实时里的 PE/市值兜底
    """
    code = str(code).zfill(6)
    js = _em_get("https://push2.eastmoney.com/api/qt/stock/get",
                 {"secid": em_secid(code),
                  "fields": "f57,f58,f84,f85,f116,f117,f127,f162,f167,f173"})
    if js and js.get("data"):
        d = js["data"]
        out = {}
        if d.get("f58"):
            out["股票简称"] = d["f58"]
        if d.get("f127"):
            out["行业"] = d["f127"]
        for k, key in [("总市值", "f116"), ("流通市值", "f117"),
                       ("总股本", "f84"), ("流通股", "f85")]:
            v = d.get(key)
            if isinstance(v, (int, float)) and v:
                out[k] = f"{v/1e8:.2f}亿"
        for k, key in [("市盈率(动)", "f162"), ("市净率", "f167")]:
            v = d.get(key)
            if isinstance(v, (int, float)) and v:
                out[k] = round(v / 100, 2)
        if out:
            return out

    try:
        import akshare as ak
        df = _retry(lambda: ak.stock_individual_info_em(symbol=code),
                    attempts=2, delay=1.0)
        return dict(zip(df["item"], df["value"]))
    except Exception:  # noqa: BLE001
        pass

    # ③ 腾讯兜底（至少有 PE / 名称）
    r = tx_realtime(code)
    if r:
        out = {"股票简称": r.get("名称", "")}
        if r.get("市盈率"):
            out["市盈率(动)"] = r["市盈率"]
        return {k: v for k, v in out.items() if v}
    return {}


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """取列，缺失时返回等长 NaN Series（避免统一取列时 None 炸掉）。"""
    if name in df.columns:
        return df[name]
    return pd.Series([np.nan] * len(df), index=df.index)


def _dt(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _dividend_em(code: str) -> pd.DataFrame | None:
    """东财 datacenter 分红明细**直连**（不用 akshare 的包装版）。

    为什么自己直连：
    1. akshare 的 `stock_fhps_detail_em` 在 `result` 为 null（该股从未分红，
       例如 688981 中芯国际）时会抛 `TypeError: 'NoneType' object is not
       subscriptable`，把「确无分红」误报成「接口挂了」。
    2. 它还带 tqdm 进度条 + 分页循环，包在 Streamlit 里会污染日志。

    返回值语义：
      - 网络失败       → None
      - 确无分红记录   → 空 DataFrame（`success=False` / `message=返回数据为空`）
    """
    js = _em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                 {"sortColumns": "REPORT_DATE", "sortTypes": "-1",
                  "pageSize": "500", "pageNumber": "1",
                  "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
                  "quoteColumns": "", "source": "WEB", "client": "WEB",
                  "filter": f'(SECURITY_CODE="{code}")'},
                 timeout=10)
    if js is None:
        return None                      # 网络失败，交给兜底源
    if js.get("success") is False or not js.get("result"):
        return pd.DataFrame()            # 接口通了，但确实没记录
    data = (js["result"] or {}).get("data") or []
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    pick = lambda k: pd.to_numeric(df.get(k), errors="coerce")  # noqa: E731
    out = pd.DataFrame({
        "报告期": _dt(df.get("REPORT_DATE")),
        "预案公告日": _dt(df.get("PLAN_NOTICE_DATE")),
        "股权登记日": _dt(df.get("EQUITY_RECORD_DATE")),
        "除权除息日": _dt(df.get("EX_DIVIDEND_DATE")),
        "分红方案": df.get("IMPL_PLAN_PROFILE", "").fillna(""),
        # 接口给的是「每 10 股派息元数」，除以 10 才是每股
        "每股派息(元)": (pick("PRETAX_BONUS_RMB") / 10).round(4),
        "每10股送转": pick("BONUS_IT_RATIO"),
        "股息率(%)": (pick("DIVIDENT_RATIO") * 100).round(3),
        "每股收益(元)": pick("BASIC_EPS").round(3),
        "进度": df.get("ASSIGN_PROGRESS", "").fillna(""),
        "最新公告日期": _dt(df.get("NOTICE_DATE")),
    })
    return out.sort_values("报告期", ascending=False).reset_index(drop=True)


def dividend_history(code: str, years: int = 10) -> pd.DataFrame:
    """分红送配历史（每股派息 / 股息率 / 股权登记日 / 除权除息日）。

    链：① **东财 datacenter 直连**（0.2s，字段最全，能区分"确无分红"）
        → ② akshare `stock_fhps_detail_em`（包装版，作为冗余）
        → ③ 巨潮 `stock_dividend_cninfo`（无股息率，用 NaN 占位）

    ⚠️ 遵守「换三只票交叉验证」铁律，确认不是忽略 code 的假接口：
    600519 每10股派 280.24 元 / 601899 每10股派 4.2 元 / 000811 10转3派1.5 元，
    三者完全不同。

    返回列（**日期保留 datetime，NaT = 尚未确定**，格式化交给展示层）：

        报告期 / 期间 / 分红方案 / 每股派息(元) / 股息率(%) / 每10股送转
        / 股权登记日 / 除权除息日 / 进度 / 每股收益(元)

    注意：最后一行常常是「预披露 / 董事会预案」，此时日期为 NaT、金额缺失，
    展示层必须能容忍，别直接 `str()` 出 `NaT`。
    """
    code = str(code).zfill(6)
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(years=years)

    src = None
    try:
        src = _dividend_em(code)
    except Exception as e:  # noqa: BLE001
        log.warning("dividend em direct failed for %s: %s", code, e)

    # ② akshare 包装版（直连失败时兜底）
    if src is None:
        try:
            import akshare as ak
            df = _retry(lambda: ak.stock_fhps_detail_em(symbol=code),
                        attempts=2, delay=1.0)
            if isinstance(df, pd.DataFrame) and len(df):
                src = pd.DataFrame({
                    "报告期": _dt(_col(df, "报告期")),
                    "预案公告日": _dt(_col(df, "预案公告日")),
                    "股权登记日": _dt(_col(df, "股权登记日")),
                    "除权除息日": _dt(_col(df, "除权除息日")),
                    "分红方案": _col(df, "现金分红-现金分红比例描述").fillna(""),
                    "每股派息(元)": (_num(_col(df, "现金分红-现金分红比例"))
                                     / 10).round(4),
                    "每10股送转": _num(_col(df, "送转股份-送转总比例")),
                    "股息率(%)": (_num(_col(df, "现金分红-股息率"))
                                  * 100).round(3),
                    "每股收益(元)": _num(_col(df, "每股收益")).round(3),
                    "进度": _col(df, "方案进度").fillna(""),
                    "最新公告日期": _dt(_col(df, "最新公告日期")),
                })
        except Exception as e:  # noqa: BLE001
            log.warning("dividend em akshare failed for %s: %s", code, e)

    if src is not None and len(src):
        src = src[src["报告期"] >= cutoff] if src["报告期"].notna().any() else src
        src = src.copy()
        # 直连版已经排好序，这里统一保证「期间」插在第二列
        if "期间" in src.columns:
            src = src.drop(columns="期间")
        src.insert(1, "期间", src["报告期"].map(period_label))
        return src.sort_values("报告期", ascending=False).reset_index(drop=True)
    if src is not None and not len(src):
        return src  # 接口明确说「没有记录」，不要再往下兜底兜出一堆错的

    # ③ 巨潮 cninfo（无股息率，用 NaN 占位）
    try:
        import akshare as ak
        c = _retry(lambda: ak.stock_dividend_cninfo(symbol=code),
                   attempts=2, delay=1.0)
        if isinstance(c, pd.DataFrame) and len(c):
            rep = _col(c, "报告时间").astype(str).str.extract(r"(\d{4})")[0]
            rep_dt = _dt(rep + "-12-31")       # 年报/季报统一落在对应年度
            out = pd.DataFrame({
                "报告期": rep_dt,
                "分红方案": _col(c, "实施方案分红说明").fillna(""),
                "每股派息(元)": (_num(_col(c, "派息比例")) / 10).round(4),
                "股息率(%)": np.nan,
                "每10股送转": (_num(_col(c, "送股比例")).fillna(0)
                               + _num(_col(c, "转增比例")).fillna(0)),
                "股权登记日": _dt(_col(c, "股权登记日")),
                "除权除息日": _dt(_col(c, "除权日")),
                "进度": _col(c, "分红类型").fillna(""),
                "每股收益(元)": np.nan,
            })
            out = out[out["报告期"] >= cutoff] if rep_dt.notna().any() else out
            out["期间"] = _col(c, "报告时间").astype(str).replace({"nan": "—"})
            return (out.sort_values("报告期", ascending=False)
                       .reset_index(drop=True))
    except Exception as e:  # noqa: BLE001
        log.warning("dividend cninfo failed for %s: %s", code, e)

    return pd.DataFrame()


def period_label(v) -> str:
    """报告期 → 中文期间标签：2025年报 / 2025三季报 / 2025半年报 / 2025一季报。"""
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return "—"
    y, m = ts.year, ts.month
    return {12: f"{y}年报", 9: f"{y}三季报",
            6: f"{y}半年报", 3: f"{y}一季报"}.get(m, f"{y}年{m}月")


def sina_fund_flow(code: str, days: int = 10) -> pd.DataFrame | None:
    """新浪个股资金流（vip.stock.finance.sina.com.cn）。

    字段：opendate 日期 / trade 收盘 / changeratio 涨跌幅 / turnover 换手
    / netamount 主力净额 / r0_net 超大单净额 / ratioamount 主力净占比
    （单位为元）
    """
    txt = _http_get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={max(days, 20)}"
        f"&sort=opendate&asc=0&daima={_tx_symbol(code)}",
        _SINA_HEADERS, timeout=10)
    if not txt or not txt.strip().startswith("["):
        return None
    try:
        arr = json.loads(txt)
        if not arr:
            return None

        def n(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        rows = [{
            "日期": x.get("opendate"),
            "收盘": n(x.get("trade")),
            "涨跌幅": round(n(x.get("changeratio")) * 100, 2),
            "换手率": n(x.get("turnover")),
            "主力净额": n(x.get("netamount")),
            "主力净占比": round(n(x.get("ratioamount")) * 100, 2),
            "超大单净额": n(x.get("r0_net")),
        } for x in arr]
        df = pd.DataFrame(rows)
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.sort_values("日期").tail(days).reset_index(drop=True)
        df["日期"] = df["日期"].dt.strftime("%Y-%m-%d")
        return df
    except Exception:  # noqa: BLE001
        return None


def fund_flow_recent(code: str, days: int = 10) -> pd.DataFrame:
    """个股近 N 日资金流（主力/超大单净额 + 净占比）。

    链：① 东财 push2 fflow daykline 直连
        → ② **新浪 MoneyFlow.ssl_qsfx_zjlrqs**（本机实测稳定可用）
        → ③ akshare 个股资金流 → ④ akshare 资金流排行
    全部失败返回空表，调用方降级提示（不抛异常）。
    """
    code = str(code).zfill(6)
    js = _em_get("https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get",
                 {"lmt": 0, "klt": 101, "secid": em_secid(code),
                  "fields1": "f1,f2,f3,f7",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"})
    if js and js.get("data") and js["data"].get("klines"):
        rows = []
        for line in js["data"]["klines"]:
            p = line.split(",")
            # f51 日期 / f52 主力净额 / f53 小单 / f54 中单 / f55 大单
            # f56 超大单 / f57 主力净占比 / f62 收盘价 / f63 涨跌幅
            rows.append({
                "日期": p[0],
                "主力净额": float(p[1] or 0),
                "超大单净额": float(p[5] or 0),
                "大单净额": float(p[4] or 0),
                "中单净额": float(p[3] or 0),
                "小单净额": float(p[2] or 0),
                "主力净占比": float(p[6] or 0),
                "收盘": float(p[11]) if len(p) > 11 and p[11] else None,
                "涨跌幅": float(p[12]) if len(p) > 12 and p[12] else None,
            })
        return pd.DataFrame(rows).tail(days)

    # ② 新浪（本机最稳）
    df = sina_fund_flow(code, days)
    if df is not None and len(df):
        return df

    import akshare as ak
    keep = ["日期", "收盘价", "涨跌幅", "主力净流入-净额",
            "主力净流入-净占比", "超大单净流入-净额"]
    try:
        df = _retry(lambda: ak.stock_individual_fund_flow(
            stock=code, market=market_of(code)), attempts=2, delay=1.0)
        df = df.sort_values("日期").tail(days)
        return df[[c for c in keep if c in df.columns]]
    except Exception:  # noqa: BLE001
        pass
    try:
        df = _retry(lambda: ak.stock_individual_fund_flow_rank(indicator="今日"),
                    attempts=2, delay=1.0)
        hit = df[df["代码"].astype(str).str.zfill(6) == code]
        if len(hit):
            return hit[[c for c in df.columns if c in keep or c in ("代码", "名称")]]
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def lhb_recent(code: str, days: int = 60) -> pd.DataFrame:
    """近 N 日龙虎榜记录（按代码过滤），带重试。"""
    import akshare as ak
    from datetime import datetime, timedelta
    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = _retry(lambda: ak.stock_lhb_detail_em(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d")), attempts=2, delay=1.0)
        df = df[df["代码"].astype(str).str.zfill(6) == str(code).zfill(6)]
        return df.sort_values("上榜日", ascending=False)
    except Exception:
        return pd.DataFrame()
