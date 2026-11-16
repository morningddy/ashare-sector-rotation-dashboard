# -*- coding: utf-8 -*-
"""Alpha Radar · A股新闻-个股联动雷达。

导入即生效的环境修正（必须在导入 akshare / requests 之前完成）：

Windows 上常见两种代理陷阱，都会让国内行情接口 ConnectionError / ProxyError：

1. **注册表残留代理**：IE/系统设置里留着未运行的代理地址，requests 通过
   `getproxies()` 自动拾取，curl 不读注册表所以表现不同。
2. **环境变量代理**：某些工具（Clash/加速器/容器）注入了
   `HTTP_PROXY=http://127.0.0.1:xxxxx`，进程退出后变量仍在。

本模块显式把国内数据域写进 `NO_PROXY`，并在检测到代理变量指向本机回环地址
（代理已失效）时直接清空代理变量。用户显式配置的外网代理会被保留。
"""
import os
import re

# 国内行情/新闻数据域（东财、财联社、交易所、同花顺、新浪）
DATA_DOMAINS = [
    "eastmoney.com", ".eastmoney.com",
    "push2.eastmoney.com", "push2his.eastmoney.com",
    "datacenter-web.eastmoney.com", "datacenter.eastmoney.com",
    "cls.cn", ".cls.cn",
    "bse.cn", ".bse.cn",
    "sse.com.cn", ".sse.com.cn",
    "szse.cn", ".szse.cn",
    "10jqka.com.cn", ".10jqka.com.cn",
    "shizixi.com", ".shizixi.com",
    "sina.com.cn", ".sina.com.cn", "sinajs.cn", ".sinajs.cn",
    "163.com", ".163.com",
]

_LOOPBACK = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?$", re.I)

# ---- 1) 清掉指向本机回环的失效代理 ----
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    _v = os.environ.get(_k)
    if _v and _LOOPBACK.match(_v):
        os.environ.pop(_k, None)

# ---- 2) 合并 NO_PROXY ----
# ⚠️ 不要写成 NO_PROXY="*"：requests.getproxies() 会返回 {'no': '*'}，
# 把 http/https 键挤掉，代理判断进入异常分支，反而更容易连不上。
# 必须用逗号分隔的域名白名单。
_ALL = DATA_DOMAINS + ["*"] if False else DATA_DOMAINS
_cur = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
_parts = [p.strip() for p in _cur.split(",") if p.strip() and p.strip() != "*"]
for _d in DATA_DOMAINS:
    if _d not in _parts:
        _parts.append(_d)
_final = ",".join(_parts)

os.environ["NO_PROXY"] = _final
os.environ["no_proxy"] = _final
