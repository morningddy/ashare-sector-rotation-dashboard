# -*- coding: utf-8 -*-
"""新闻 → 股票映射与多空判定（零成本本地实现）。

三层匹配：
1. 显式代码：正则抓 6 位代码（含 .SH/.SZ/.BJ 后缀）
2. 公司简称：A 股代码表精确子串匹配（仅取 >=3 字简称，避免误报）
3. 题材关键词：手写词表 → 概念板块名（成分股按需拉取并落盘缓存）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CODELIST_CACHE = DATA_DIR / "codelist.json"
CONCEPT_DIR = DATA_DIR / "concepts"

# ---------------- 多空词表 ----------------
BULL_WORDS = [
    "中标", "签约", "预增", "扭亏", "回购", "增持", "涨价", "获批", "订单",
    "扩产", "满产", "涨价函", "业绩预增", "净利增长", "超预期", "突破", "创新高",
    "战略合作", "收购", "并购", "分红", "提价", "涨价预期", "供不应求", "加码",
    "获批上市", "集采中标", "出海", "出口订单", "获得专利", "技术突破",
]
BEAR_WORDS = [
    "减持", "质押", "预亏", "处罚", "立案", "退市", "警示", "问询", "违规",
    "商誉减值", "业绩下滑", "亏损", "停产", "事故", "诉讼", "仲裁", "解禁",
    "股东减持", "高管辞职", "评级下调", "下调", "砍单", "库存积压", "价格战",
    "财报不及预期", "低于预期", "终止", "暂停", "风险提示",
]

CONCEPT_KEYWORDS = {
    # 半导体 / 电子
    "玻璃基板": "玻璃基板", "TGV": "玻璃基板", "先进封装": "先进封装",
    "半导体": "半导体概念", "芯片": "半导体概念", "晶圆": "半导体概念",
    "存储": "存储芯片", "光刻": "半导体概念", "封测": "半导体概念",
    "碳化硅": "第三代半导体", "氮化镓": "第三代半导体", "三代半": "第三代半导体",
    "CPO": "CPO概念", "光模块": "CPO概念", "光通信": "CPO概念",
    "PCB": "PCB", "覆铜板": "PCB", "面板": "面板", "OLED": "OLED",
    "消费电子": "消费电子", "MiniLED": "MiniLED", "MicroLED": "MicroLED",
    # AI / 算力
    "算力": "算力概念", "AI": "AI算力", "人工智能": "AI算力",
    "大模型": "ChatGPT概念", "ChatGPT": "ChatGPT概念", "AIGC": "AIGC概念",
    "数据中心": "数据中心", "IDC": "数据中心", "液冷": "液冷概念",
    "温控": "液冷概念", "服务器": "服务器", "边缘计算": "边缘计算",
    "机器人": "机器人概念", "人形机器人": "人形机器人", "具身智能": "人形机器人",
    "机器视觉": "机器视觉", "减速器": "减速器",
    # 新能源
    "光伏": "光伏概念", "硅料": "光伏概念", "组件": "光伏概念", "逆变器": "逆变器",
    "风电": "风能", "海上风电": "风能", "储能": "储能", "固态电池": "固态电池",
    "锂电池": "锂电池", "锂电": "锂电池", "钠电池": "钠离子电池",
    "氢能": "氢能源", "燃料电池": "氢能源", "充电桩": "充电桩",
    "新能源车": "新能源汽车", "汽车零部件": "汽车零部件", "一体化压铸": "一体化压铸",
    # 医药
    "创新药": "创新药", "中药": "中药", "医疗器械": "医疗器械",
    "CXO": "CRO", "疫苗": "疫苗", "GLP-1": "减肥药", "减肥药": "减肥药",
    "医美": "医美概念", "集采": "医疗器械",
    # 消费
    "白酒": "白酒概念", "食品饮料": "食品饮料", "预制菜": "预制菜",
    "免税": "免税概念", "旅游": "旅游酒店", "猪价": "猪肉", "猪肉": "猪肉",
    "鸡苗": "鸡肉", "化妆品": "化妆品",
    # 金融 / 地产
    "券商": "证券", "银行": "银行", "保险": "保险", "信托": "多元金融",
    "房地产": "房地产开发", "地产": "房地产开发", "基建": "基建工程",
    # 资源 / 化工
    "稀土": "稀土永磁", "永磁": "稀土永磁", "锂矿": "锂矿", "碳酸锂": "锂矿",
    "铜": "铜概念", "黄金": "黄金概念", "金价": "黄金概念", "煤炭": "煤炭",
    "石油": "石油行业", "天然气": "天然气", "磷化工": "磷化工",
    "氟化工": "氟化工", "钛白粉": "钛白粉", "纯碱": "纯碱",
    # 军工 / 航天
    "军工": "军工", "国防": "军工", "航空发动机": "航空发动机",
    "卫星": "卫星导航", "北斗": "卫星导航", "低空经济": "低空经济",
    "飞行汽车": "飞行汽车/eVTOL", "eVTOL": "飞行汽车/eVTOL", "无人机": "无人机",
    # 科技 / 通信
    "华为": "华为概念", "鸿蒙": "鸿蒙概念", "苹果": "苹果概念",
    "信创": "信创", "国产替代": "国产软件", "数据要素": "数据要素",
    "网络安全": "网络安全", "数字货币": "数字货币", "区块链": "区块链",
    "5G": "5G概念", "6G": "6G概念", "物联网": "物联网", "量子": "量子科技",
    # 其他热门
    "核电": "核电", "电力": "电力行业", "电网": "电网设备",
    "碳中和": "碳中和", "环保": "环保行业", "养殖": "养殖业",
    "种业": "种业", "转基因": "转基因", "水利": "水利建设",
    "养老": "养老概念", "生育": "婴童概念", "跨境电商": "跨境电商",
}


def _ensure_data_dir() -> None:
    CONCEPT_DIR.mkdir(parents=True, exist_ok=True)


def load_codelist() -> pd.DataFrame:
    """A 股代码表 [code,name]，本地缓存 1 天。多源容错：交易所列表 → 东财快照。

    ⚠️ 云端沙箱网络受限时 akshare / 东财兜底可能全部失败——此时
    **回退用过期缓存**（代码表晚几天更新几乎无影响，
    总比返回空表导致所有新闻的个股命中归零强）。
    """
    _ensure_data_dir()
    cached = None
    if CODELIST_CACHE.exists():
        try:
            cached = pd.read_json(CODELIST_CACHE, dtype={"code": str})
        except Exception:
            cached = None
        if cached is not None and len(cached) and \
                time.time() - CODELIST_CACHE.stat().st_mtime < 86400:
            return cached

    def _norm(df: pd.DataFrame) -> pd.DataFrame | None:
        if df is None or not len(df):
            return None
        code_col = next((c for c in df.columns if str(c).lower() in ("code", "代码")), None)
        name_col = next((c for c in df.columns if str(c).lower() in ("name", "名称")), None)
        if not code_col or not name_col:
            return None
        out = df[[code_col, name_col]].copy()
        out.columns = ["code", "name"]
        out["code"] = out["code"].astype(str).str.zfill(6)
        return out.drop_duplicates("code")

    df = None
    try:
        import akshare as ak
        df = _norm(ak.stock_info_a_code_name())  # 含北交所，偶发接口变动/被墙
    except Exception:
        df = None
    if df is None:
        try:  # 兜底：东财全市场快照（沪深，覆盖 99% 场景）
            import akshare as ak
            df = _norm(ak.stock_zh_a_spot_em())
        except Exception:
            df = None
    if df is None:
        if cached is not None and len(cached):
            # 联网刷新失败 → 用过期缓存兜底（空表会让全部个股命中消失）
            return cached
        return pd.DataFrame(columns=["code", "name"])
    df.to_json(CODELIST_CACHE, orient="records", force_ascii=False)
    return df


_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 简称匹配排除词：这些词本身是常用词，出现在新闻里不代表提到该公司
_NAME_BLOCKLIST = {
    "中国", "建设", "发展", "科技", "股份", "集团", "国际", "实业", "投资",
    "能源", "电力", "环保", "生物", "医药", "汽车", "电子", "机械", "化工",
    "钢铁", "有色", "传媒", "文化", "健康", "食品", "农业", "林业", "渔业",
    "信息", "通信", "软件", "智能", "数据", "网络", "新材", "材料", "高科",
    "控股", "有限", "公司", "中心", "基地", "新区", "开发", "工程", "设计",
    "东方", "北方", "南方", "西部", "东部", "长江", "黄河", "珠江",
}

_names_cache: dict[int, tuple[list[tuple[str, str]], dict[str, str], dict[str, int]]] = {}

# 模糊匹配时的「龙头优先」表：同长度候选无法靠字数区分时用它定序
# （"紫金" → 紫金矿业 / 紫金银行 / 紫金龙净，用户 99% 想看紫金矿业）
_PRIORITY_NAMES = {
    "贵州茅台", "五粮液", "泸州老窖", "山西汾酒", "宁德时代", "比亚迪", "紫金矿业",
    "中国平安", "招商银行", "工商银行", "农业银行", "兴业银行", "美的集团",
    "格力电器", "海尔智家", "海康威视", "立讯精密", "工业富联", "隆基绿能",
    "通威股份", "阳光电源", "万华化学", "恒瑞医药", "迈瑞医疗", "药明康德",
    "爱尔眼科", "长江电力", "中国神华", "陕西煤业", "兖矿能源", "洛阳钼业",
    "北方华创", "中芯国际", "韦尔股份", "兆易创新", "三安光电", "京东方",
    "TCL科技", "中兴通讯", "科大讯飞", "海光信息", "三一重工", "潍柴动力",
    "徐工机械", "上汽集团", "长城汽车", "长安汽车", "赛力斯", "伊利股份",
    "海天味业", "中国中免", "牧原股份", "温氏股份", "东方财富", "中信证券",
    "同花顺", "顺丰控股", "京沪高铁", "中国移动", "中国电信", "中国石油",
    "中国石化", "中国船舶", "中国核电", "中国建筑", "中国中铁", "泡泡玛特",
    "小米集团", "腾讯控股", "阿里巴巴", "美团", "京东集团", "冰轮环境",
}

# 短简称后紧跟这些字 → 判为普通词，不算命中（"数字人" + "民币"）
_TAIL_BLOCK = set("民币率数化型式法条额度级别类项种类组队会站场区市省县镇乡村路桥")


def _prepare(codelist: pd.DataFrame):
    """预处理代码表：拆出可匹配简称 + 代码→名称索引，结果按 id 缓存。

    剔除：
      - 2 字以下（太短易误报）
      - 含全角空格/特殊字符的（如"万  科Ａ"）
      - 纯行业通用词（见 _NAME_BLOCKLIST）
    """
    key = id(codelist)
    if key in _names_cache:
        return _names_cache[key]

    code2name = dict(zip(codelist["code"].astype(str), codelist["name"].astype(str)))
    pairs: list[tuple[str, str]] = []
    # 记录原始顺序：同长度候选用它 tiebreak（交易所表序里主流票更靠前）
    order: dict[str, int] = {}
    for idx, (name, code) in enumerate(
            zip(codelist["name"].astype(str), codelist["code"].astype(str))):
        # 清理全角空格与字母后缀（"万  科Ａ" → "万科"）
        clean = re.sub(r"[\s\u3000]", "", name)
        clean = re.sub(r"[A-ZＡ-Ｚ]$", "", clean)
        if len(clean) < 2 or clean in _NAME_BLOCKLIST:
            continue
        pairs.append((clean, code.zfill(6)))
        order[clean] = idx
    # 长名优先，避免"中矿资源"被更短的"中矿"抢先
    pairs.sort(key=lambda x: (-len(x[0]), order.get(x[0], 0)))
    _names_cache[key] = (pairs, code2name, order)
    return _names_cache[key]


def resolve_code(query: str, codelist: pd.DataFrame | None = None,
                 prefer: list[str] | None = None) -> dict | None:
    """把用户输入解析成股票。接受：

        601899        纯 6 位代码
        601899.SH     带后缀
        紫金矿业      公司简称（精确 → 再模糊）
        紫金          简称片段
        600519 贵州茅台  代码+名称

    prefer 可传优先代码列表（如用户自选股），命中时排到最前。
    成功返回 {code, name}，失败返回 None。
    """
    if not query:
        return None
    codelist = codelist if codelist is not None else load_codelist()
    if not len(codelist):
        return None
    text = str(query).strip()
    code2name = dict(zip(codelist["code"].astype(str), codelist["name"].astype(str)))
    rank = {str(c).zfill(6): i for i, c in enumerate(prefer or [])}

    # 1) 找 6 位代码
    m = _CODE_RE.search(text)
    if m:
        code = m.group(1).zfill(6)
        if code in code2name:
            return {"code": code, "name": code2name[code]}
        return None  # 有代码但不在表里（ETF/退市等）

    # 2) 按名称找：先精确，再子串
    clean = re.sub(r"[\s\u3000]", "", text)
    pairs, _, order = _prepare(codelist)
    for name, code in pairs:
        if name == clean:
            return {"code": code, "name": code2name[code]}
    # 子串匹配：定序优先级 —— 自选股 > 龙头表 > 名称以查询词开头 > 名称更长
    frag = [(n, c) for n, c in pairs if clean and (clean in n or n in clean)]
    if frag:
        frag.sort(key=lambda x: (
            rank.get(x[1], 9999),
            0 if x[0] in _PRIORITY_NAMES else 1,
            not x[0].startswith(clean),
            -len(x[0]),
            order.get(x[0], 99999),
        ))
        name, code = frag[0]
        return {"code": code, "name": code2name[code]}
    return None


def suggest_stocks(query: str, codelist: pd.DataFrame | None = None,
                   limit: int = 4, prefer: list[str] | None = None) -> list[dict]:
    """输入联想：返回 [{code,name}]，用于个股分析页的快捷点选。

    与 resolve_code 共用同一套定序权重（自选 > 龙头 > 前缀 > 长度），
    保证「输入框解析结果」和「建议列表第一项」永远是同一只票。
    """
    if not query or not str(query).strip():
        return []
    codelist = codelist if codelist is not None else load_codelist()
    if not len(codelist):
        return []
    text = re.sub(r"[\s\u3000]", "", str(query).strip())
    pairs, code2name, order = _prepare(codelist)
    rank = {str(c).zfill(6): i for i, c in enumerate(prefer or [])}

    # 先粗筛：代码前缀 / 名称前缀 / 名称包含
    cand: list[tuple[str, str]] = []
    for name, code in pairs:
        if (text.isdigit() and code.startswith(text)) or (not text.isdigit() and (
                name.startswith(text) or text in name)):
            cand.append((name, code))
    cand.sort(key=lambda x: (
        rank.get(x[1], 9999),
        0 if x[0] in _PRIORITY_NAMES else 1,
        not x[0].startswith(text) if not text.isdigit() else 0,
        len(x[0]),
        order.get(x[0], 99999),
    ))
    out, seen = [], set()
    for name, code in cand:
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": code2name[code]})
        if len(out) >= limit:
            break
    return out


def match_stocks(text: str, codelist: pd.DataFrame | None = None,
                 with_concepts: bool = False, max_hits: int = 30) -> list[dict]:
    """从一段新闻文本中匹配关联股票，返回 [{code,name,reason}]。

    三层：
      1. 显式 6 位代码（最准）
      2. 全市场公司简称子串匹配（含 2 字名，带通用词黑名单与长名优先）
      3. 题材关键词 → 概念板块成分股（需 with_concepts=True）
    """
    if not text:
        return []
    codelist = codelist if codelist is not None else load_codelist()
    if not len(codelist):
        return []
    pairs, code2name, _ = _prepare(codelist)
    hits: dict[str, dict] = {}

    # 1) 显式代码
    for code in set(_CODE_RE.findall(text)):
        code = code.zfill(6)
        if code in code2name:
            hits[code] = {"code": code, "name": code2name[code], "reason": "代码命中"}

    # 2) 全市场简称子串匹配
    for name, code in pairs:
        if code in hits:
            continue
        if name not in text:
            continue
        # 防子串误伤：短名（2-3字）若紧跟"币/率/数/化/型/式"等词根，
        # 多半是普通词而非公司（"数字人" vs "数字人民币"）
        if len(name) <= 3:
            idx = text.find(name)
            tail = text[idx + len(name): idx + len(name) + 1]
            if tail in _TAIL_BLOCK:
                continue
        hits[code] = {"code": code, "name": code2name.get(code, name),
                      "reason": "公司名命中"}
        if len(hits) >= max_hits:
            break

    # 3) 题材关键词 → 概念成分股
    if with_concepts:
        for kw, concept in CONCEPT_KEYWORDS.items():
            if kw in text and len(hits) < max_hits:
                for code, name in concept_members(concept)[:8]:
                    if code not in hits:
                        hits[code] = {"code": code, "name": name,
                                      "reason": f"题材「{kw}」"}
                        if len(hits) >= max_hits:
                            break
    return list(hits.values())


def related_stocks(text: str, codelist: pd.DataFrame | None = None,
                   max_hits: int = 8) -> list[dict]:
    """新闻的「高置信关联个股」：只取代码命中 + 公司简称命中（不含题材扩写）。

    Tab1 卡片用这个，避免题材词一次性带出几十只导致噪音。
    """
    out = match_stocks(text, codelist, with_concepts=False, max_hits=max_hits)
    return [h for h in out if h["reason"] in ("代码命中", "公司名命中")]


def concept_members(concept: str, top: int = 8) -> list[tuple[str, str]]:
    """概念板块成分股（按最新涨跌幅取前 N），磁盘缓存 1 天。"""
    _ensure_data_dir()
    cache = CONCEPT_DIR / f"{concept}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return [tuple(x) for x in data[:top]]
    try:
        import akshare as ak
        df = ak.stock_board_concept_cons_em(symbol=concept)
        df["代码"] = df["代码"].astype(str).str.zfill(6)
        pairs = list(zip(df["代码"], df["名称"]))[:top]
        cache.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
        return pairs
    except Exception:
        return []


def sentiment(text: str) -> tuple[str, list[str]]:
    """粗粒度多空判定，返回 (标签, 命中词)。"""
    bull = [w for w in BULL_WORDS if w in text]
    bear = [w for w in BEAR_WORDS if w in text]
    if bull and not bear:
        return "利好", bull
    if bear and not bull:
        return "利空", bear
    if bull and bear:
        return "多空交织", bull + bear
    return "中性", []
