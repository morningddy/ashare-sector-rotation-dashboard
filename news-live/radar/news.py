# -*- coding: utf-8 -*-
"""新闻雷达数据层：财联社电报 + 东财全球快讯（函数名多版本容错）。

统一输出记录：{source, time, content, sentiment, hits:[{code,name,reason}]}
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pandas as pd

from .mapping import load_codelist, match_stocks, sentiment

log = logging.getLogger(__name__)

NEWS_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
NEWS_TTL = 300  # 秒，新闻落盘缓存 5 分钟


def _cache_path(days: int, with_concepts: bool) -> Path:
    """缓存文件名必须带上「回溯天数 + 是否扩展题材」，否则换范围会串到旧数据。

    曾经只有一个 `news_cache.json`：把回溯范围做成可调之后，
    7 天的结果会被 1 天的缓存覆盖回来，表现为「改了设置数字却不变」。
    """
    tag = f"news_cache_d{days}{'_c' if with_concepts else ''}.json"
    return NEWS_CACHE_DIR / tag


def _load_cache(days: int, with_concepts: bool) -> list[dict] | None:
    path = _cache_path(days, with_concepts)
    try:
        if path.exists() and time.time() - path.stat().st_mtime < NEWS_TTL:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data:
                log.info("news cache hit (%d records)", len(data))
                return data
    except Exception as e:  # noqa: BLE001
        log.warning("news cache read failed: %s", e)
    return None


def _save_cache(records: list[dict], days: int, with_concepts: bool) -> None:
    try:
        path = _cache_path(days, with_concepts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, ensure_ascii=False),
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("news cache write failed: %s", e)


def _try(ak, names: list[str], *args, **kwargs) -> pd.DataFrame | None:
    """依次尝试多个 akshare 函数名（版本间接口常改名）。"""
    for n in names:
        fn = getattr(ak, n, None)
        if fn is None:
            continue
        try:
            df = fn(*args, **kwargs)
            if isinstance(df, pd.DataFrame) and len(df):
                log.info("news source ok: %s (%d rows)", n, len(df))
                return df
        except Exception as e:  # noqa: BLE001
            log.warning("news source %s failed: %s", n, e)
    return None


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        for col in df.columns:
            if c in str(col):
                return col
    return None


# ---------------- 东财快讯：多栏目合并（突破 200 条硬顶） ----------------
# ⚠️ 三个实测出来的坑，都是「参数看着能用、实际被忽略」那一类：
#  1. `pageSize` 是**服务端上限 200**——传 500/1000/2000 都只回 200，改参数无效。
#  2. `sortEnd` 翻页游标**会被忽略**：传上一页末条的 showTime，仍返回第一页内容。
#     → 和历史上 getNewsByColumns 忽略 code 是同一类坑，别信它。
#  3. 唯一有效的扩容办法是**换 fastColumn**：每个栏目给自己的 200 条，
#     akshare 的 `stock_info_global_em` 只用了 102 一个栏目，所以它永远只有 200 条。
#     扫描 100~130 后合并去重可得 4600+ 条，个股覆盖从 ~70 只涨到 ~550 只（8x）。
_EM_FASTNEWS_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
_EM_FASTNEWS_COLS = list(range(100, 131))
_EM_CN_PREFIX = {"0", "1"}          # stockList 前缀：0=深市 1=沪市，其余是板块/港股/美股/基金


def _em_fastnews(timeout: int = 6, days: int = 3) -> list[dict]:
    """直连东财 `getFastNewsList`，多栏目合并去重（并行版）。

    比走 akshare 多两样东西：① 条数更多（4600+ vs 200）；
    ② 返回里的 `stockList` 是**接口自带的个股标注**（比名字文本匹配精度高），
    存进 `_tags` 字段供 mapping 层合并。

    ⚠️ 一定要按 `days` 过滤旧新闻：某些栏目的 200 条会滚到很早以前
    （实测混进过 2019 年的消息），不滤会把陈年旧闻当成今日电报喂给用户。
    时间戳无法解析的保守保留，宁可不滤掉。

    ⚠️ 云端/受限网络下单个栏目连接可能长时间挂起，串行抓 31 个栏目
    最坏要 6 分钟以上。改为线程池并行（10 并发 × 6s 超时），
    最坏 ~20 秒内全部栏目出结果，失败的栏目直接跳过。
    """
    import requests
    import concurrent.futures as cf
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=days)
    hdr = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}

    def _one_col(col: int) -> list[dict]:
        params = {"client": "web", "biz": "web_724", "fastColumn": str(col),
                  "sortEnd": "", "pageSize": "200", "req_trace": "1"}
        try:
            r = requests.get(_EM_FASTNEWS_URL, params=params, headers=hdr,
                             timeout=timeout, proxies={})
            js = r.json()
        except Exception as e:  # noqa: BLE001  单个栏目失败不影响其它
            log.warning("fastnews col %s failed: %s", col, e)
            return []
        rows = []
        for it in ((js.get("data") or {}).get("fastNewsList") or []):
            title = str(it.get("title") or "").strip()
            summary = str(it.get("summary") or "").strip()
            body = (f"{title}。{summary}".strip("。").strip()
                    if summary else title)
            if not body:
                continue
            ts = str(it.get("showTime") or "").strip()
            try:                               # 旧新闻直接丢掉
                if ts and datetime.strptime(
                        ts[:19], "%Y-%m-%d %H:%M:%S") < cutoff:
                    continue
            except ValueError:
                pass
            art = str(it.get("code") or "").strip()
            rows.append({
                "source": "东财快讯", "time": ts, "content": body,
                "url": (f"https://finance.eastmoney.com/a/{art}.html"
                        if art else ""),
                "_tags": list(it.get("stockList") or []),
            })
        return rows

    rows: list[dict] = []
    ex = cf.ThreadPoolExecutor(max_workers=10)
    try:
        for part in ex.map(_one_col, _EM_FASTNEWS_COLS):
            rows += part
    except Exception as e:  # noqa: BLE001
        log.warning("fastnews parallel fetch failed: %s", e)
    finally:
        ex.shutdown(wait=False)

    # 多栏目结果合并去重（同一条新闻会出现在多个栏目，按标题+时间去重）
    out, seen = [], set()
    for r in rows:
        key = (r["content"].split("。")[0], r["time"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _cls_direct(days: int = 1, timeout: int = 15) -> list[dict]:
    """直连财联社电报接口（v1/roll/get_roll_list，md5(sha1()) 签名）。

    akshare 的 stock_info_global_cls 只回 4 列（标题/内容/日期/时间），
    **丢掉了 id**，导致财联社新闻无法跳转原文。这里直连官方接口，
    用 id 构造 `https://www.cls.cn/detail/{id}` 原文链接，
    并按 ctime 过滤回溯天数、用 last_time 游标翻页。

    失败返回 []（上层回退 akshare，牺牲链接但不牺牲内容）。
    """
    import hashlib
    import time as _time
    import urllib.parse
    from datetime import datetime, timedelta

    import requests

    cutoff = datetime.now() - timedelta(days=days)
    hdr = {"User-Agent": _UA, "Referer": "https://www.cls.cn/telegraph"}
    rows, seen = [], set()
    last_time = int(_time.time())
    for _page in range(6):                 # 每页 20 条，6 页覆盖 1 天足够
        params = {
            "app": "CailianpressWeb", "category": "",
            "last_time": last_time, "os": "web",
            "refresh_type": "1", "rn": "20", "sv": "8.4.6",
        }
        # 签名：urlencode 后 sha1 → hexdigest 再 md5（顺序按 akshare 的字段序）
        params["sign"] = hashlib.md5(
            hashlib.sha1(urllib.parse.urlencode(params).encode("utf-8"))
            .hexdigest().encode("utf-8")).hexdigest()
        try:
            r = requests.get("https://www.cls.cn/v1/roll/get_roll_list",
                             params=params, headers=hdr, timeout=timeout,
                             proxies={})
            roll = ((r.json().get("data") or {}).get("roll_data")) or []
        except Exception as e:  # noqa: BLE001
            log.warning("cls direct failed: %s", e)
            return rows if rows else []
        if not roll:
            break
        oldest = None
        for it in roll:
            nid = str(it.get("id") or "").strip()
            title = str(it.get("title") or "").strip()
            content = str(it.get("content") or "").strip()
            body = f"{title}。{content}".strip("。").strip() if content else title
            if not body or not nid:
                continue
            try:
                dt = datetime.fromtimestamp(int(it.get("ctime")))
            except Exception:  # noqa: BLE001
                dt = None
            if dt is not None:
                if dt < cutoff:
                    continue
                ts = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts = ""
            if nid in seen:
                continue
            seen.add(nid)
            rows.append({
                "source": "财联社", "time": ts, "content": body,
                "url": f"https://www.cls.cn/detail/{nid}",
            })
            if it.get("ctime"):
                c = int(it["ctime"])
                oldest = c if oldest is None else min(oldest, c)
        if oldest is None or len(rows) >= 500:
            break
        last_time = oldest
        _time.sleep(0.2)
    return rows


def _cls_with_timeout(timeout_s: int = 45):
    """财联社电报抓取（akshare 兜底）加硬超时。

    akshare 的财联社接口内部 requests 不带 timeout，在受限网络
    （云端沙箱 / 代理半死）下会**无限挂起**，把整个刷新线程卡死。
    放到独立线程里跑，超时直接放弃（挂起的线程不再等待）。
    """
    import concurrent.futures as cf

    def _run():
        import akshare as ak
        fn = (getattr(ak, "stock_telegraph_cls", None)
              or getattr(ak, "stock_info_global_cls", None))
        return fn() if fn else None

    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_run)
        try:
            return fut.result(timeout=timeout_s)
        except cf.TimeoutError:
            log.warning("cls telegraph timed out after %ss", timeout_s)
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("cls telegraph failed: %s", e)
        return None
    finally:
        ex.shutdown(wait=False)


def _parse_stock_tags(tags: list) -> list[str]:
    """把接口自带的 `stockList`（形如 `0.000002` / `1.600519`）还原成 6 位 A 股代码。

    前缀语义：`0`=深市、`1`=沪市；`90`/`1007`=BK 板块、`105`/`106`=美股、
    `116`=港股、`150`/`999`=基金 —— 除了 0/1 其余全部丢弃。
    """
    out = []
    for raw in tags or []:
        s = str(raw)
        pfx, code = s.split(".", 1) if "." in s else ("?", s)
        if pfx in _EM_CN_PREFIX and code.isdigit() and len(code) == 6:
            out.append(code)
    return out


def _normalize(df: pd.DataFrame, source: str) -> list[dict]:
    if df is None or not len(df):
        return []
    tcol = _pick_col(df, ["发布时间", "日期时间", "时间", "datetime", "publish"])
    dcol = _pick_col(df, ["发布日期"])
    ccol = _pick_col(df, ["内容", "电报内容", "摘要", "content"])
    if ccol is None:
        return []
    out = []
    for _, r in df.iterrows():
        content = str(r[ccol]).strip()
        if not content or content == "nan":
            continue
        ts = str(r[tcol]).strip() if tcol else ""
        if dcol and tcol:  # 财联社：日期+时间分两列，拼接成完整时间戳
            ts = f"{str(r[dcol]).strip()} {ts}"
        out.append({"source": source, "time": ts, "content": content})
    return out


def fetch_all_news(with_concepts: bool = False,
                   codelist: pd.DataFrame | None = None,
                   use_cache: bool = True,
                   days: int = 7) -> tuple[list[dict], list[str]]:
    """抓全部新闻源并做映射/多空标注。返回 (records, errors)。

    days: 回溯天数。东财每个栏目固定给最近 200 条，所以**单栏目永远只有 200 条**；
          要更多就得在多栏目间取并集再往前翻，条数随 days 增长：
          1 天 ≈197 条/68 只、3 天 ≈599 条/142 只、**7 天 ≈1300 条/449 只**、
          14 天 ≈1863 条/533 只。默认 7 天，是「够看」和「不拖慢」的平衡点。

    use_cache=True 时优先读 5 分钟内的落盘缓存（含映射结果），
    避免 Streamlit 每次 rerun 都重新抓取 + 重新映射。
    缓存键含 (days, with_concepts)，改范围不会串数据。
    """
    if use_cache and not with_concepts:  # 概念扩展模式命中结果不同，不复用缓存
        cached = _load_cache(days, with_concepts)
        if cached is not None:
            return cached, []

    import akshare as ak  # noqa: F401  仅保留导入以预热依赖
    codelist = codelist if codelist is not None else _codelist_safe()
    records: list[dict] = []
    errors: list[str] = []

    cls_rows = _cls_direct(days=days)   # 直连带签名接口，带原文链接
    if cls_rows:
        records += cls_rows
    else:
        # 回退 akshare（无链接）；硬超时防沙箱/受限网络挂死
        cls = _cls_with_timeout(timeout_s=45)
        if cls is None:
            errors.append("财联社电报接口不可用")
        records += _normalize(cls, "财联社")

    # 东财快讯：优先直连多栏目（7 天 1300+ 条），失败才退回 akshare（单栏目 200 条）
    em_rows = _em_fastnews(days=days)
    if em_rows:
        records += em_rows
    else:
        em = _try(ak, ["stock_info_global_em"])
        if em is None:
            errors.append("东财快讯接口不可用")
        else:
            log.warning("multi-column fastnews empty, fell back to akshare (200 rows)")
        records += _normalize(em, "东财")

    for rec in records:
        try:
            rec["sentiment"], rec["hit_words"] = sentiment(rec["content"])
            rec["hits"] = match_stocks(rec["content"], codelist,
                                       with_concepts=with_concepts)
            # 合并接口自带的个股标注（比文本匹配准，两者互补各自漏约 10 只 / 396 只）
            for code in _parse_stock_tags(rec.pop("_tags", None)):
                if any(h["code"] == code for h in rec["hits"]):
                    continue
                nm = _codelist_name(codelist, code)
                # 只有能查到名字才算命中，避免把已退市/不在表内的代码塞进去
                if nm:
                    rec["hits"].append({"code": code, "name": nm,
                                        "reason": "电报标注"})
        except Exception as e:  # 单条异常不拖垮整个 feed
            log.warning("record mapping failed: %s", e)
            rec["sentiment"], rec["hit_words"], rec["hits"] = "中性", [], []
    records.sort(key=lambda x: x["time"], reverse=True)

    if use_cache and records:
        _save_cache(records, days, with_concepts)
    return records, errors


def _codelist_name(codelist: pd.DataFrame | None, code: str) -> str:
    """按代码从代码表取名称；查不到返回空串（避免塞进无效代码）。"""
    if codelist is None or not len(codelist):
        return ""
    try:
        hit = codelist.loc[codelist["code"].astype(str).str.zfill(6) == code,
                           "name"]
        return str(hit.iloc[0]) if len(hit) else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 个股专属新闻 ----------------

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")
_EM_NEWS_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

_STOCK_NEWS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_STOCK_NEWS_TTL = 600  # 10 分钟


def _search_name(name: str) -> str:
    """清洗公司名作检索词：去空格/全角转半角/去掉 A、B、U、W 后缀。

    代码表里有一批票名带奇怪后缀和空格，例如「万  科Ａ」「深振业Ａ」，
    原样丢给东财搜索会 **匹配不到任何东西 → 接口返回当日公共要闻兜底**
    （表现为某只票拿到一堆和它无关的新闻）。所以必须先清洗。
    """
    s = re.sub(r"[\s\u3000]", "", str(name or ""))
    s = "".join(chr(ord(c) - 0xFEE0) if "\uff01" <= c <= "\uff5e" else c for c in s)
    core = re.sub(r"[ABUW]$", "", s)
    return core if len(core) >= 2 else s


def _relevance(title: str, content: str, code: str, core: str) -> str:
    """判定一条资讯与这只股票的相关度。

    - strong：标题命中，或正文开头 60 字内命中（说明真的是讲这只票）
    - weak  ：只在正文深处被顺带提一句（盘面复盘、行业综述里常见）
    - none  ：全文都没提 → 丢弃（这类就是接口的兜底垃圾结果）

    ⚠️ 没有这一步，像「万科」这种高频词会拿到一堆「每日复盘」长文，
    用户看到的是「每只票都有新闻但全不相关」——和历史上
    getNewsByColumns 忽略 code 的表现一模一样。
    """
    head = content[:60]
    if core and (core in title or core in head) or code in (title or head):
        return "strong"
    if core and (core in content or code in content):
        return "weak"
    return "none"


def _stock_news_em_feed(code: str, limit: int = 20) -> list[dict]:
    """东财 np-listapi 个股资讯流（search-api-web 在云端沙箱不可达时的备用源）。

    按 mTypeAndCode（0.深/1.沪 + 代码）直接取该股资讯流，接口自带个股
    过滤，无需再做相关度判定。字段：Art_Title/Art_ShowTime/Art_Url。
    """
    import requests
    code = str(code).zfill(6)
    mtype = ("1." if code.startswith("6") else "0.") + code
    try:
        r = requests.get("https://np-listapi.eastmoney.com/comm/web/getListInfo",
                         params={"client": "web", "mTypeAndCode": mtype,
                                 "type": "1", "pageSize": str(max(limit, 20)),
                                 "pageIndex": "1"},
                         headers=_EM_NEWS_HEADERS, timeout=10, proxies={})
        lst = ((r.json().get("data") or {}).get("list")) or []
    except Exception as e:  # noqa: BLE001
        log.warning("em feed stock news failed %s: %s", code, e)
        return []
    rows = []
    for it in lst:
        title = str(it.get("Art_Title") or "").strip()
        if not title:
            continue
        label, words = sentiment(title)
        rows.append({"time": str(it.get("Art_ShowTime") or "")[:19],
                     "source": "个股资讯", "content": title,
                     "sentiment": label, "hit_words": words, "hits": [],
                     "url": str(it.get("Art_Url") or ""),
                     "relevance": "strong"})
    return rows[:limit]


def _stock_announcements(code: str, limit: int = 20) -> list[dict]:
    """东财个股公告流（第三备用源）。"""
    import requests
    code = str(code).zfill(6)
    try:
        r = requests.get("https://np-anotice-stock.eastmoney.com/api/security/ann",
                         params={"page_size": str(max(limit, 20)),
                                 "page_index": "1", "ann_type": "A",
                                 "stock_list": code},
                         headers=_EM_NEWS_HEADERS, timeout=10, proxies={})
        lst = (r.json().get("data") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001
        log.warning("em announcements failed %s: %s", code, e)
        return []
    rows = []
    for it in lst:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        art = str(it.get("art_code") or "").strip()
        label, words = sentiment(title)
        rows.append({"time": str(it.get("notice_date") or "")[:19],
                     "source": "公司公告", "content": title,
                     "sentiment": label, "hit_words": words, "hits": [],
                     "url": (f"https://data.eastmoney.com/notices/detail/"
                             f"{code}/{art}.html" if art else ""),
                     "relevance": "strong"})
    return rows[:limit]


def fetch_stock_news(code: str, name: str = "", limit: int = 25,
                     ttl: int = _STOCK_NEWS_TTL) -> list[dict]:
    """拉取单只股票的专属新闻（补齐电报流的覆盖盲区）。

    背景：实时电报流（财联社+东财快讯）通常只有 200 余条，涉及个股 30 多只，
    绝大多数中小票**当天没有任何消息**——但它们的个股资讯里其实有内容。
    所以个股分析页 / 全市场普查选中某票时必须再单独拉一次专属新闻。

    源：东财全文搜索 `search-api-web` 按公司名检索资讯。
    ⚠️ 踩过的坑（两个都要记住）：
    1. 不要用 `np-listapi getNewsByColumns` + `code` 参数，那个接口会
       **忽略 code 返回公共财经要闻**（三只票内容完全一样的那种）。
    2. 即使用全文搜索，`sort="time"` 时也几乎等于兜底要闻——必须用
       `sort="default"`（相关性排序），否则「深振业」这类冷门票
       30 条结果里 **0 条**真的提到它（实测）。
    3. 检索词要清洗后再搜，见 `_search_name`。

    返回统一格式: {time, source, content, sentiment, hit_words, hits, url,
    relevance}，relevance 为 strong/weak，strong 排在前面。
    """
    code = str(code).zfill(6)
    hit = _STOCK_NEWS_CACHE.get(code)
    if hit and time.time() - hit[0] < ttl:
        return hit[1][:limit]
    if not name:
        try:
            cl = load_codelist()
            m = cl[cl["code"].astype(str) == code]
            name = str(m.iloc[0]["name"]).strip() if len(m) else code
        except Exception:  # noqa: BLE001
            name = code

    import json
    import urllib.parse
    import requests
    rows: list[dict] = []
    keyword = _search_name(name)
    try:
        param = {
            "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
            "client": "web", "clientVersion": "curr", "clientType": "web",
            "param": {"cmsArticleWebOld": {
                # sort 必须是 default（相关性）：time 排序会退化成当日要闻兜底
                "searchScope": "default", "sort": "default",
                "pageIndex": 1, "pageSize": max(limit * 2, 30),
                "preTag": "<em>", "postTag": "</em>"}},
        }
        url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=x&param="
               + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
        r = requests.get(url, headers=_EM_NEWS_HEADERS, timeout=12, proxies={})
        txt = r.text if r.status_code == 200 else ""
        js = {}
        if txt:
            body = txt[txt.find("(") + 1: txt.rfind(")")] if "(" in txt else txt
            js = json.loads(body)
        strong, weak = [], []
        for it in (js.get("result") or {}).get("cmsArticleWebOld") or []:
            title = re.sub(r"</?em>", "", (it.get("title") or "")).strip()
            content = re.sub(r"</?em>", "", (it.get("content") or "")).strip()
            body = f"{title}。{content}" if content else title
            if not body.strip("。 "):
                continue
            rel = _relevance(title, content, code, keyword)
            if rel == "none":
                continue
            label, words = sentiment(body)
            rec = {"time": (it.get("date") or "")[:19], "source": "个股资讯",
                   "content": body, "sentiment": label, "hit_words": words,
                   "hits": [], "url": it.get("url", ""), "relevance": rel}
            (strong if rel == "strong" else weak).append(rec)
        # 强相关（真的是讲这只票的）在前，正文提及的在后；段内保留相关性顺序
        rows = (strong + weak)[:limit]
    except Exception as e:  # noqa: BLE001
        log.warning("stock news fetch failed for %s: %s", code, e)

    if not rows:
        # search-api-web 不可用/无结果（云端沙箱封了该域）→ 备用源链：
        # ① np-listapi 个股资讯流 → ② 公告流
        rows = (_stock_news_em_feed(code, limit=limit)
                or _stock_announcements(code, limit=limit))

    if rows:
        _STOCK_NEWS_CACHE[code] = (time.time(), rows)
    elif hit:  # 接口失败时留旧缓存兜底
        return hit[1][:limit]
    return rows[:limit]


def stock_news(records: list[dict], code: str, name: str = "",
               max_items: int = 40, with_stock_news: bool = True) -> dict:
    """筛出与某只股票相关的新闻，按相关度分两级。

    返回 {"direct": [...], "topic": [...]}，每项在原记录上加 "hit" 字段。

    - direct：代码命中 / 公司名命中 / 文本命中（高置信，用户最想看）
    - topic ：题材概念命中（该股属于这个概念板块，间接相关）

    ⚠️ 文本兜底很重要：mapping 的长名优先策略偶尔会让某只票在被更长的
    名字抢占后不出现在 hits 里，但代码/公司名确实出现在正文中。
    """
    code = str(code).zfill(6)
    nm = (name or "").strip()
    direct: list[dict] = []
    topic: list[dict] = []

    for r in records:
        hit = None
        for h in r.get("hits", []):
            if str(h["code"]).zfill(6) == code:
                hit = h
                break
        if hit is None and nm and len(nm) >= 2:
            txt = r.get("content", "")
            if code in txt or nm in txt:
                hit = {"code": code, "name": nm, "reason": "文本命中"}
        if hit is None:
            continue
        item = {**r, "hit": hit}
        if hit["reason"] in ("代码命中", "公司名命中", "文本命中"):
            direct.append(item)
        else:
            topic.append(item)

    key = lambda x: x.get("time", "")  # noqa: E731
    direct.sort(key=key, reverse=True)
    topic.sort(key=key, reverse=True)
    direct = direct[:max_items]
    topic = topic[:max_items]

    # 电报流命中为 0 时，拉个股专属新闻补齐（电报流只覆盖 30 余只热点票）
    dedicated: list[dict] = []
    if with_stock_news and not direct:
        for r in fetch_stock_news(code, name, limit=max_items):
            r = dict(r)
            rel = r.get("relevance", "strong")
            r["hits"] = [{"code": code, "name": name or code,
                          "reason": ("个股专属·标题命中" if rel == "strong"
                                     else "个股专属·正文提及")}]
            dedicated.append(r)
    return {"direct": direct, "topic": topic, "dedicated": dedicated}


def news_summary(records: list[dict]) -> dict:
    """新闻列表的情绪分布统计。"""
    return {
        "total": len(records),
        "bull": sum(1 for r in records if r["sentiment"] == "利好"),
        "bear": sum(1 for r in records if r["sentiment"] == "利空"),
        "mix": sum(1 for r in records if r["sentiment"] == "多空交织"),
        "neu": sum(1 for r in records if r["sentiment"] == "中性"),
    }


def full_market_coverage(records: list[dict],
                         codelist: pd.DataFrame | None = None) -> pd.DataFrame:
    """全市场每只股票的消息面统计（不只被提到的那些）。

    用户诉求：「要全部的股票，不是只有关联的」。电报流只覆盖 0.6% 的票，
    所以这里把 **5569 只全部列出**，被提到的填充统计，没被提到的填 0。

    返回列：代码/名称/新闻数/利好/利空/净情绪/热度/命中方式
    - 热度：新闻数加权分（利好+2、利空-2、中性+1，再叠加新闻数），用于排序
    - 排序：有消息的按新闻数降序在前，无消息的按代码序在后
    """
    codelist = codelist if codelist is not None else _codelist_safe()
    if not len(codelist):
        return pd.DataFrame(columns=["代码", "名称", "新闻数", "利好", "利空",
                                     "净情绪", "热度", "命中方式"])

    # 聚合：code → 计数
    agg: dict[str, dict] = {}
    for r in records:
        sent = r.get("sentiment", "中性")
        for h in r.get("hits", []):
            c = str(h["code"]).zfill(6)
            a = agg.setdefault(c, {"新闻数": 0, "利好": 0, "利空": 0,
                                   "多空交织": 0, "中性": 0, "命中方式": set()})
            a["新闻数"] += 1
            a[sent] = a.get(sent, 0) + 1
            a["命中方式"].add(h.get("reason", ""))

    rows = []
    for _, row in codelist.iterrows():
        code = str(row["code"]).zfill(6)
        # 代码表里有一批「万  科Ａ」这种夹空格+全角后缀的名字，展示前先规整
        name = re.sub(r"[\s\u3000]", "", str(row["name"]))
        a = agg.get(code)
        if a:
            rows.append({
                "代码": code, "名称": name, "新闻数": a["新闻数"],
                "利好": a["利好"], "利空": a["利空"],
                "净情绪": a["利好"] - a["利空"],
                "热度": a["利好"] * 2 - a["利空"] * 2 + a["多空交织"] + a["新闻数"],
                "命中方式": "、".join(sorted(x for x in a["命中方式"] if x)),
            })
        else:
            rows.append({"代码": code, "名称": name, "新闻数": 0,
                         "利好": 0, "利空": 0, "净情绪": 0, "热度": 0,
                         "命中方式": ""})
    df = pd.DataFrame(rows)
    # 有消息的在前（按新闻数、热度降序），无消息的按代码序
    df["_has"] = (df["新闻数"] > 0).astype(int)
    df = (df.sort_values(["_has", "新闻数", "热度", "代码"],
                         ascending=[False, False, False, True])
            .drop(columns="_has").reset_index(drop=True))
    return df


def _codelist_safe() -> pd.DataFrame:
    try:
        return load_codelist()
    except Exception as e:  # noqa: BLE001
        log.error("codelist load failed: %s", e)
        return pd.DataFrame(columns=["code", "name"])


def filter_records(records: list[dict], mode: str = "全部",
                   watch_codes: set[str] | None = None,
                   keyword: str = "") -> list[dict]:
    watch_codes = watch_codes or set()
    kw = (keyword or "").strip()
    out = []
    for r in records:
        codes = {h["code"] for h in r["hits"]}
        if mode == "利好" and r["sentiment"] not in ("利好", "多空交织"):
            continue
        if mode == "利空" and r["sentiment"] not in ("利空", "多空交织"):
            continue
        if mode == "持仓相关" and not (codes & watch_codes):
            continue
        if mode == "仅关联个股" and not codes:
            continue
        if kw and kw not in r["content"]:
            continue
        out.append(r)
    return out
