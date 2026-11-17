# -*- coding: utf-8 -*-
"""实时财经电报网页 · 服务端。

复用 alpha-radar 的数据层（radar.news / radar.mapping）：
- 财联社电报（akshare）
- 东财快讯 getFastNewsList 多栏目直连（100~130，合并去重）
- 个股专属资讯（东财全文搜索 search-api-web）
- 多空情绪判定 + 个股命中标注

启动：start.bat（或 python server.py）
访问：http://127.0.0.1:8510
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SELF_DIR = Path(__file__).resolve().parent
GP_DIR = SELF_DIR.parent                                  # D:/codex/GP
ALPHA_DIR = GP_DIR / "alpha-radar"
# 部署形态：项目目录内自带 radar/ 与 data/（从 alpha-radar 复制，随包上传）；
# 本地开发时若项目内没有 radar 包，则回退用 alpha-radar 的。
sys.path.insert(0, str(SELF_DIR))
if (ALPHA_DIR / "radar").is_dir():
    sys.path.append(str(ALPHA_DIR))

import radar  # noqa: F401,E402  导入即修正 NO_PROXY（必须在 requests/akshare 之前）
from radar.news import fetch_all_news, fetch_stock_news, news_summary  # noqa: E402

# 云端沙箱会注入 PORT 环境变量并要求绑定 0.0.0.0；本地无 PORT 时保持 127.0.0.1:8510
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
PORT = int(os.environ.get("PORT", "8510"))
STATIC_DIR = SELF_DIR / "static"
REFRESH_SECONDS = 60                                     # 后台自动刷新周期

# ---------------- 简易限流（每 IP 每窗口请求数） ----------------
RATE_LIMIT = 60            # 窗口内最大请求数（页面本身 1 次/分钟，足够宽松）
RATE_WINDOW = 60.0         # 窗口秒数
_api_hits: dict[str, tuple[float, int]] = {}
_rate_lock = threading.Lock()


def _client_ip(handler) -> str:
    """取真实客户端 IP：反向代理后面要看 X-Forwarded-For / X-Real-IP。"""
    fwd = handler.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    real = handler.headers.get("X-Real-IP") or ""
    if real:
        return real.strip()
    return handler.client_address[0] if handler.client_address else "unknown"


def _allow_request(ip: str) -> tuple[bool, int]:
    """固定窗口计数。返回 (是否放行, 剩余等待秒数)。"""
    now = time.time()
    with _rate_lock:
        start, cnt = _api_hits.get(ip, (now, 0))
        if now - start >= RATE_WINDOW:
            start, cnt = now, 0
        cnt += 1
        _api_hits[ip] = (start, cnt)
        if len(_api_hits) > 2000:          # 顺带清理过期条目，防内存膨胀
            for k, (s, _c) in list(_api_hits.items()):
                if now - s >= RATE_WINDOW:
                    _api_hits.pop(k, None)
        wait = max(0, int(RATE_WINDOW - (now - start)))
        return cnt <= RATE_LIMIT, wait


# ---------------- 行情中转（/api/relay） ----------------
# 用途：预览面板 / 内嵌 webview / 公司网络会拦截对行情 CDN 的直连（CSP 或出口策略），
# 浏览器端 fetch 全部失败。此时页面改走 http://127.0.0.1:8510/api/relay?u=<原地址>，
# 由本机 Python 侧代为请求，再把原始字节原样回传（GBK 编码保持不动，前端自己解码）。
RELAY_HOSTS = {
    # 腾讯
    "qt.gtimg.cn", "web.ifzq.gtimg.cn", "ifzq.gtimg.cn", "proxy.finance.qq.com",
    "smartbox.gtimg.cn", "web.sqt.gtimg.cn",
    # 东方财富
    "push2.eastmoney.com", "push2delay.eastmoney.com", "push2his.eastmoney.com",
    "push2ex.eastmoney.com", "searchapi.eastmoney.com", "datacenter-web.eastmoney.com",
    "np-anotice-stock.eastmoney.com",
    # 新浪（备源）
    "hq.sinajs.cn", "money.finance.sina.com.cn",
}
RELAY_MAX_BYTES = 4 * 1024 * 1024          # 单次响应上限，防被塞大文件
RELAY_TIMEOUT = 15
RELAY_LIMIT = 3000                         # 中转是浏览器发起的批量请求，限流放宽
_relay_hits: dict[str, tuple[float, int]] = {}
_relay_lock = threading.Lock()
# 显式绕开系统代理：公司/本机代理常把 https 打成 502，行情接口必须直连
_relay_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_relay_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _allow_relay(ip: str) -> tuple[bool, int]:
    now = time.time()
    with _relay_lock:
        start, cnt = _relay_hits.get(ip, (now, 0))
        if now - start >= RATE_WINDOW:
            start, cnt = now, 0
        cnt += 1
        _relay_hits[ip] = (start, cnt)
        wait = max(0, int(RATE_WINDOW - (now - start)))
        return cnt <= RELAY_LIMIT, wait


def _relay_fetch(target: str) -> tuple[int, bytes, str]:
    """按白名单代取远端资源，返回 (状态码, 原始字节, Content-Type)。"""
    p = urlparse(target)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http/https allowed")
    host = (p.hostname or "").lower()
    if host not in RELAY_HOSTS:
        raise PermissionError(f"host not allowed: {host}")
    req = urllib.request.Request(target, headers={
        "User-Agent": _relay_ua,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://gu.qq.com/",
    })
    try:
        with _relay_opener.open(req, timeout=RELAY_TIMEOUT) as r:
            body = r.read(RELAY_MAX_BYTES + 1)
            if len(body) > RELAY_MAX_BYTES:
                raise ValueError("response too large")
            return r.status, body, r.headers.get("Content-Type") or "application/octet-stream"
    except urllib.error.HTTPError as e:
        body = e.read()[:2048] or b""
        return e.code, body, e.headers.get("Content-Type") or "text/plain; charset=utf-8"

# ---------------- 全局新闻池（后台线程维护，days=1 实时档） ----------------
_pool: list[dict] = []
_pool_ts: float = 0.0
_pool_lock = threading.Lock()
_pool_errors: list[str] = []
_refreshing = False
_last_refresh_result = ""          # 最近一次后台刷新的结果（诊断用）


def _load_disk_cache() -> list[dict]:
    """读 days=1 落盘缓存做启动兜底（过滤 48 小时以上的陈旧记录）。"""
    p = ALPHA_DIR / "data" / "news_cache_d1.json"
    if not p.exists():
        p = SELF_DIR / "data" / "news_cache_d1.json"
    try:
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8")) or []
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d")
        return [r for r in raw
                if str(r.get("time", ""))[:10] >= cutoff]
    except Exception:  # noqa: BLE001
        pass
    return []


def _refresh_pool() -> None:
    """后台刷新 days=1 的实时电报流（绕过磁盘缓存，保证新鲜度）。"""
    global _pool, _pool_ts, _pool_errors, _refreshing
    with _pool_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        records, errors = fetch_all_news(days=1, use_cache=False)
        with _pool_lock:
            if records:
                _pool = records
                _pool_ts = time.time()
                _pool_errors = errors
                globals()["_last_refresh_result"] = (
                    f"ok {len(records)} records at "
                    f"{time.strftime('%H:%M:%S')} errors={errors}")
            else:
                globals()["_last_refresh_result"] = (
                    f"empty at {time.strftime('%H:%M:%S')} errors={errors}")
    except Exception as e:  # noqa: BLE001
        globals()["_last_refresh_result"] = (
            f"FAIL at {time.strftime('%H:%M:%S')}: {type(e).__name__}: {e}")
        print(f"[news-live] refresh failed: {e}")
    finally:
        with _pool_lock:
            _refreshing = False


def _ensure_refresh_async() -> None:
    """池子为空时立即在后台线程开抓（不阻塞请求线程）。"""
    threading.Thread(target=_refresh_pool, daemon=True).start()


def _pool_worker() -> None:
    while True:
        _refresh_pool()
        time.sleep(REFRESH_SECONDS)


def _records_for(days: int) -> tuple[list[dict], list[str], bool]:
    """取指定回溯天数的记录。

    days=1：优先内存实时池 → 池空先读磁盘缓存兜底并触发后台抓取（不阻塞）。
            只有连磁盘缓存都没有时才同步抓一次（首次部署且无缓存的情况）。
    days>1：按需同步抓取（走 alpha-radar 的 5 分钟磁盘缓存）。

    返回 (records, errors, loading)，loading=True 表示前端应延时重试。
    """
    if days <= 1:
        with _pool_lock:
            if _pool:
                return _pool, _pool_errors, False
        # 池子空：磁盘缓存兜底 + 立即触发后台刷新
        cached = _load_disk_cache()
        _ensure_refresh_async()
        if cached:
            with _pool_lock:
                if not _pool:            # 别覆盖后台已抓到的新数据
                    globals()["_pool"] = cached
                    globals()["_pool_ts"] = time.time()
            return cached, [], False
        # 全空：同步抓一次（仅首次部署会发生，之后总有缓存）
        records, errors = fetch_all_news(days=days, use_cache=True)
        if records:
            with _pool_lock:
                if not _pool:
                    globals()["_pool"] = records
                    globals()["_pool_ts"] = time.time()
        return records, errors, not records
    records, errors = fetch_all_news(days=days, use_cache=True)
    return records, errors, not records


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802  静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许 file:// 直接打开的页面跨域调用本服务（静态预览兜底）
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra=None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def do_GET(self):  # noqa: N802
        """异常兜底：任何接口报错都返回 JSON 500，绝不掐断连接让前端拿到空响应。"""
        try:
            self._route_get()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._json({"error": "server internal error",
                            "detail": traceback.format_exc(limit=3)}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _route_get(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        # 限流只作用于 API（静态首页不受影响），防止被刷爆免费数据源
        # 中转接口有独立（更宽松）的限额，见下面 /api/relay 分支
        if u.path.startswith("/api/") and u.path != "/api/relay":
            ok, wait = _allow_request(_client_ip(self))
            if not ok:
                self._json({"error": "rate_limited", "retry_after": wait,
                            "message": f"请求过于频繁，请 {wait} 秒后再试（每分钟限 {RATE_LIMIT} 次）"},
                           429, {"Retry-After": str(wait)})
                return

        if u.path in ("/", "/index.html"):
            f = STATIC_DIR / "index.html"
            self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        elif u.path == "/api/news":
            try:
                days = int((q.get("days") or ["1"])[0])
            except ValueError:
                days = 1
            days = max(1, min(days, 14))
            records, errors, loading = _records_for(days)
            self._json({
                "records": records,
                "summary": news_summary(records),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(_pool_ts)),
                "errors": errors,
                "days": days,
                "loading": loading,
            })

        elif u.path == "/api/stock_news":
            code = (q.get("code") or [""])[0].strip()
            if not code:
                self._json({"error": "missing code"}, 400)
                return
            rows = fetch_stock_news(code, (q.get("name") or [""])[0], limit=30)
            self._json({"records": rows, "code": code})

        elif u.path == "/api/quote":
            code = (q.get("code") or [""])[0].strip()
            if not code:
                self._json({"error": "missing code"}, 400)
                return
            try:                       # 延迟导入，quote 查询不影响主流程
                from radar.stocks import realtime_row
                row = realtime_row(code)
            except Exception as e:  # noqa: BLE001
                row = None
                print(f"[news-live] quote failed for {code}: {e}")
            self._json({"quote": row or {}, "code": code})

        elif u.path == "/api/relay":
            # 只服务本机页面：这是给"浏览器直连被拦"的本地兜底，不能当公网代理用
            peer = self.client_address[0] if self.client_address else ""
            if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._json({"error": "relay_local_only",
                            "message": "行情中转仅在 127.0.0.1 上可用"}, 403)
                return
            ok, wait = _allow_relay(_client_ip(self))
            if not ok:
                self._json({"error": "rate_limited", "retry_after": wait}, 429,
                           {"Retry-After": str(wait)})
                return
            target = (q.get("u") or [""])[0].strip()
            if not target:
                self._json({"error": "missing u", "hint": "/api/relay?u=<url encoded>"}, 400)
                return
            try:
                code, body, ctype = _relay_fetch(target)
            except PermissionError as e:
                self._json({"error": "host_not_allowed" if "not allowed" in str(e) else str(e)},
                           403)
                return
            except Exception as e:  # noqa: BLE001
                self._json({"error": "relay_failed", "detail": f"{type(e).__name__}: {e}"[:300]},
                           502)
                return
            # 原样回传字节（GBK 保持不动），前端用 TextDecoder 自行解码
            self._send(200 if code == 200 else 502, body, ctype,
                       {"X-Relay-Status": str(code), "X-Relay-Host": urlparse(target).hostname or ""})

        elif u.path == "/api/health":
            with _pool_lock:
                n = len(_pool)
                ts = _pool_ts
            self._json({"ok": True, "pool_size": n,
                        "relay": True, "relay_hosts": len(RELAY_HOSTS),
                        "pool_age_sec": round(time.time() - ts) if ts else None,
                        "last_refresh": _last_refresh_result,
                        "refreshing": _refreshing})

        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> None:
    threading.Thread(target=_pool_worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[news-live] serving at http://{HOST}:{PORT}")
    print(f"[news-live] static dir: {STATIC_DIR}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
