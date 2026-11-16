#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
刷新仪表盘内置的全量 A 股本地索引（代码 + 名称 + 汉字→拼音首字母表）。

用法：
    py -3.10 build_stock_index.py                    # 就地更新 HTML
    py -3.10 build_stock_index.py --file xxx.html    # 指定文件
    py -3.10 build_stock_index.py --dry              # 只打印统计不写文件

数据源：akshare 的 stock_info_a_code_name()（交易所口径，含北交所）。
生成两块数据：
    STK_LIST  —— 每行 "6位代码+名称"，数组 join('\\n')
    STK_PY    —— 汉字→拼音首字母分组串，形如 a阿啊埃|b八巴..."
作用：搜索完全本地化，断网也能按 代码前缀 / 中文名 / 拼音首字母 命中。
依赖：pip install akshare pypinyin
"""
import argparse
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HTML = os.path.join(HERE, "ashare-sector-rotation-dashboard.html")


def fetch_rows():
    import akshare as ak
    df = ak.stock_info_a_code_name()
    rows = []
    for _, r in df.iterrows():
        code = str(r["code"]).strip()
        name = unicodedata.normalize("NFKC", str(r["name"]).strip())
        name = re.sub(r"\s+", "", name)
        if re.fullmatch(r"\d{6}", code) and name:
            rows.append((code, name))
    rows = sorted(set(rows))
    return rows


def build_py_table(rows):
    from pypinyin import Style, pinyin
    chars = set()
    for _, n in rows:
        chars.update(n)
    groups = {}
    for ch in sorted(c for c in chars if "\u4e00" <= c <= "\u9fff"):
        try:
            p = pinyin(ch, style=Style.FIRST_LETTER, errors="ignore")
            letter = p[0][0][0].lower() if p and p[0] and p[0][0] else ""
        except Exception:
            letter = ""
        if letter and "a" <= letter <= "z":
            groups.setdefault(letter, []).append(ch)
    return "|".join(k + "".join(v) for k, v in sorted(groups.items())), len(chars)


def render_blocks(rows, py):
    lines = []
    for i in range(0, len(rows), 12):
        lines.append("  " + ",".join('"%s%s"' % (c, n) for c, n in rows[i:i + 12]) + ",")
    stk_list = 'const STK_LIST = [\n' + "\n".join(lines) + '\n].join("\\n");'
    stk_py = 'const STK_PY = "%s";' % py
    return stk_list, stk_py


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_HTML)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    rows = fetch_rows()
    if len(rows) < 3000:
        sys.exit("抓到的股票数异常（%d），放弃写入" % len(rows))
    py, nchars = build_py_table(rows)
    stk_list, stk_py = render_blocks(rows, py)
    print("股票 %d 只 / 汉字 %d 个 / 拼音表 %d 字节 / 列表 %d 字节"
          % (len(rows), nchars, len(stk_py.encode()), len(stk_list.encode())))

    if a.dry:
        return

    html = open(a.file, encoding="utf-8").read()
    m1 = re.search(r"const STK_LIST = \[.*?\n\]\.join\(\"\\n\"\);", html, re.S)
    m2 = re.search(r'const STK_PY = "[^"]*";', html)
    if not (m1 and m2):
        sys.exit("未找到 STK_LIST / STK_PY 占位块，可能是首次注入，请手工插入锚点后再运行")
    html = html[:m1.start()] + stk_list + html[m1.end():]
    m2 = re.search(r'const STK_PY = "[^"]*";', html)
    html = html[:m2.start()] + stk_py + html[m2.end():]
    open(a.file, "w", encoding="utf-8").write(html)
    print("已写入 %s（%.0f KB）" % (a.file, len(html.encode()) / 1024))


if __name__ == "__main__":
    main()
