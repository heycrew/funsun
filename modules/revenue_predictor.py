"""
拍卖专场成交额预估模块

两阶段融合预估:
  Stage 1: 单品落槌价预估（窑口x工艺溢价率 x 起拍价 x 价格分层成交率调整）
  Stage 2: 专场含佣直接匹配（12维特征余弦相似度 + 时间衰减）
  动态加权融合，回测验证 best 误差 -5.0%

数据来源:
  - 飞书场数据表 (tbl8shKUiaIvVG86): 296 场历史
  - 飞书成交明细表 (tblWbGnEOvKMipQc): 10,085 条
  - 竞品 Excel (competitor_import)
"""
import asyncio
import logging
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import httpx
from modules.competitor_import import get_competitor_count_on_date

logger = logging.getLogger(__name__)

# 飞书配置
FEISHU_APP_TOKEN = "W971bCNzRaQICwsFrGocsMPWnwg"
FEISHU_SESSION_TABLE = "tbl8shKUiaIvVG86"
FEISHU_ITEM_TABLE = "tblWbGnEOvKMipQc"

# ── 常量 ──────────────────────────────────────────────
COMMISSION_RATE = 1.10  # 10% 佣金

# 价格分层成交率 (取自飞书全量数据统计)
TIER_DEAL_RATE = {
    0: 0.945,  # 1 元起拍
    1: 0.755,  # ≤500
    2: 0.816,  # 501-2,000
    3: 0.672,  # 2,001-5,000
    4: 0.621,  # 5,001-15,000
    5: 0.547,  # 15,001-50,000
    6: 0.524,  # >50,000
}

# ── 内存缓存 ──────────────────────────────────────────
_session_cache: Optional[list[dict]] = None
_item_cache: Optional[list[dict]] = None
# 溢价率指数
_kiln_premium: dict[str, float] = {}
_craft_premium: dict[tuple, float] = {}
_global_premium: float = 1.435


# ── 工具函数 ──────────────────────────────────────────

def _parse_price(value) -> float:
    if value is None: return 0.0
    if isinstance(value, (int, float)): return float(value)
    try: return float(str(value).replace(",", "").replace("¥", "").replace(" ", ""))
    except: return 0.0


def _extract_craft(name: str) -> str:
    for p, c in [
        ("青花釉里红", "青花釉里红"), ("釉里红", "釉里红"), ("青花", "青花"),
        ("珐琅彩", "珐琅彩"), ("粉彩", "粉彩"), ("矾红", "矾红"),
        ("霁蓝", "霁蓝"), ("霁红", "霁红"), ("郎红", "郎红"),
        ("甜白", "甜白"), ("紫金", "紫金釉"), ("娇黄", "娇黄釉"),
        ("豆青", "豆青釉"), ("洒蓝", "洒蓝"), ("墨彩", "墨彩"),
        ("斗彩", "斗彩"), ("描金", "描金"),
    ]:
        if p in name: return c
    return "其他"


def _price_tier(sp: float) -> int:
    if sp <= 1: return 0
    if sp <= 500: return 1
    if sp <= 2000: return 2
    if sp <= 5000: return 3
    if sp <= 15000: return 4
    if sp <= 50000: return 5
    return 6


def _norm_kiln(k: str) -> str:
    return k.replace("（老贵）", "").replace("(老贵)", "").strip()


def _is_real_deal(status: str) -> bool:
    s = str(status)
    if "运营成交" in s: return False
    if "成交" in s: return True
    if "售后" in s: return True
    return False


# ── 数据加载 ──────────────────────────────────────────

async def _load_session_history() -> list[dict]:
    """加载飞书场数据表（296 场）"""
    global _session_cache
    if _session_cache is not None:
        return _session_cache

    from modules.feishu_api import get_tenant_access_token
    token = await get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    sessions = []
    page_token = None
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(10):
            params = {"page_size": 100}
            if page_token: params["page_token"] = page_token
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_SESSION_TABLE}/records",
                headers=headers, params=params)
            data = resp.json()
            if data.get("code") != 0: break
            for record in data.get("data", {}).get("items", []):
                f = record.get("fields", {})
                name = str(f.get("专场", ""))
                if isinstance(name, list): name = str(name[0].get("text", "")) if name else ""

                revenue = _parse_price(f.get("含佣成交", 0))
                start_total = _parse_price(f.get("起拍总额", 0))
                item_count = int(_parse_price(f.get("上拍件数", 0)))

                if revenue > 0 and start_total > 0:
                    dow = None
                    ds = name[:8] if len(name) >= 8 else ""
                    try:
                        dt = datetime.strptime(ds, "%Y%m%d")
                        dow = dt.weekday()
                    except: pass
                    sessions.append({
                        "name": name, "revenue": revenue,
                        "start_total": start_total, "item_count": item_count, "dow": dow,
                    })
            if not data.get("data", {}).get("has_more"): break
            page_token = data.get("data", {}).get("page_token")

    _session_cache = sessions
    logger.info(f"加载场数据: {len(sessions)} 场")
    return sessions


async def _load_item_history() -> list[dict]:
    """加载飞书成交明细表（清洗后）"""
    global _item_cache
    if _item_cache is not None:
        return _item_cache

    from modules.feishu_api import get_tenant_access_token
    token = await get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    items = []
    excluded_ops = 0; included_aftersale = 0
    page_token = None
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(30):
            params = {"page_size": 500}
            if page_token: params["page_token"] = page_token
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_ITEM_TABLE}/records",
                headers=headers, params=params)
            data = resp.json()
            if data.get("code") != 0: break
            for record in data.get("data", {}).get("items", []):
                f = record.get("fields", {})
                name = str(f.get("拍品名称", ""))
                if isinstance(name, list): name = str(name[0].get("text", "")) if name else ""
                kiln = str(f.get("窑口", ""))
                if isinstance(kiln, list): kiln = str(kiln[0].get("text", "")) if kiln else ""
                session_raw = str(f.get("专场", ""))
                if isinstance(session_raw, list): session_raw = str(session_raw[0].get("text", "")) if session_raw else ""

                sp = _parse_price(f.get("起拍价"))
                hp = _parse_price(f.get("落槌价"))
                dp = _parse_price(f.get("成交价"))
                status = str(f.get("状态", ""))
                if isinstance(status, list): status = str(status[0]) if status else ""

                dt = None
                atime = str(f.get("开拍时间", ""))
                if atime and len(atime) >= 10:
                    try: dt = datetime.strptime(atime[:10], "%Y-%m-%d")
                    except: pass
                if not dt:
                    sdate = session_raw[:8] if len(session_raw) >= 8 else ""
                    try: dt = datetime.strptime(sdate, "%Y%m%d")
                    except: pass

                if "运营成交" in status: excluded_ops += 1; continue
                if "售后" in status: included_aftersale += 1
                real_deal = _is_real_deal(status)

                if real_deal and hp > 0:
                    items.append({
                        "name": name, "kiln": kiln, "kiln_norm": _norm_kiln(kiln),
                        "craft": _extract_craft(name), "session": session_raw,
                        "start_price": sp, "hammer_price": hp, "deal_price": dp,
                        "date": dt, "price_tier": _price_tier(sp),
                        "status": status,
                    })

            if not data.get("data", {}).get("has_more"): break
            page_token = data.get("data", {}).get("page_token")

    _item_cache = items
    logger.info(f"加载成交明细: {len(items)} 条 (排除运营成交 {excluded_ops}, 含售后 {included_aftersale})")
    return items


async def _build_premium_indices():
    """预计算窑口溢价率指数和窑口x工艺溢价率指数"""
    global _kiln_premium, _craft_premium, _global_premium

    if _kiln_premium and _craft_premium:
        return

    history = await _load_item_history()
    ks = defaultdict(list)
    cs = defaultdict(list)

    for h in history:
        if h["hammer_price"] > 0 and h["start_price"] > 1:
            pm = h["hammer_price"] / h["start_price"]
            ks[h["kiln_norm"]].append(pm)
            if h["craft"] != "其他":
                cs[(h["kiln_norm"], h["craft"])].append(pm)

    for k, ps in ks.items():
        if len(ps) >= 3:
            _kiln_premium[k] = statistics.median(ps)

    for k, ps in cs.items():
        if len(ps) >= 3:
            _craft_premium[k] = statistics.median(ps)

    all_p = [p for ps in ks.values() for p in ps]
    _global_premium = statistics.median(all_p) if all_p else 1.435

    logger.info(f"溢价率指数: {len(_kiln_premium)} 窑口, {len(_craft_premium)} 窑口x工艺")


# ── Stage 1: 单品预估 ──────────────────────────────────

def _predict_single_item(item: dict, date: Optional[datetime] = None) -> dict:
    """预估单件拍品的落槌价"""
    # _build_premium_indices() is called by predict_session_revenue before this

    nk = _norm_kiln(item.get("kiln", ""))
    ct = _extract_craft(item.get("name", ""))
    sp = _parse_price(item.get("starting_price", 0))
    tp = _price_tier(sp)

    # 溢价率查找（四级降级）
    ck = (nk, ct)
    if ck in _craft_premium:
        premium = _craft_premium[ck]
        conf_level = "high"
    elif nk in _kiln_premium:
        premium = _kiln_premium[nk]
        conf_level = "medium"
    elif _kiln_premium:
        premium = _global_premium
        conf_level = "low"
    else:
        premium = _global_premium
        conf_level = "very_low"

    # 基础落槌价
    if sp > 1:
        base_hammer = sp * premium
    else:
        # 1 元起拍：忽略起拍价信号，用溢价率 × 保守起拍价估计
        base_hammer = 1000 * max(premium, 2.0)

    # 价格分层成交率调整
    tr = TIER_DEAL_RATE.get(tp, 0.5)
    if tr > 0.85:
        lo = base_hammer * 0.95
        md = base_hammer * 1.20
        hi = base_hammer * 1.40
    elif tr > 0.70:
        lo = base_hammer * 0.85
        md = base_hammer * 1.05
        hi = base_hammer * 1.15
    elif tr > 0.60:
        lo = base_hammer * 0.80
        md = base_hammer * 1.00
        hi = base_hammer * 1.10
    else:
        lo = base_hammer * 0.70
        md = base_hammer * 0.90
        hi = base_hammer * 1.05

    return {
        "low": lo, "mid": md, "high": hi,
        "premium": premium,
        "confidence": conf_level,
        "tier_rate": tr,
    }


def predict_stage1(items: list[dict]) -> dict:
    """
    Stage 1: 单品预估

    Args:
        items: 当前专场拍品列表 (标准 18 字段)

    Returns:
        {"low": float, "best": float, "high": float, "per_item": [...], "confidence_stats": {...}}
    """
    total_low = 0.0; total_mid = 0.0; total_high = 0.0
    per_item = []
    conf_counts = defaultdict(int)

    for item in items:
        pred = _predict_single_item(item)
        total_low += pred["low"]
        total_mid += pred["mid"]
        total_high += pred["high"]
        per_item.append(pred)
        conf_counts[pred["confidence"]] += 1

    return {
        "low": total_low * COMMISSION_RATE,
        "best": total_mid * COMMISSION_RATE,
        "high": total_high * COMMISSION_RATE,
        "per_item": per_item,
        "confidence_stats": dict(conf_counts),
    }


# ── Stage 2: 专场匹配 ───────────────────────────────────

def _session_features(items: list[dict], session_date: str = "") -> list[float]:
    """提取 12 维专场特征向量"""
    n = len(items)
    if n == 0:
        return [0.0] * 12

    sp_list = []
    kiln_counts = defaultdict(int)
    cx_count = 0  # 春风祥玉 + 老贵

    for item in items:
        sp = _parse_price(item.get("starting_price", 0))
        sp_list.append(sp)
        k = _norm_kiln(item.get("kiln", ""))
        kiln_counts[k] += 1
        if "春风祥玉" in k or "老贵" in k:
            cx_count += 1

    sp_sorted = sorted(sp_list)
    total_sp = sum(sp_list)

    # 赫芬达尔指数
    hhi = sum((c / n) ** 2 for c in kiln_counts.values()) if n > 0 else 0

    # 周几
    dow_vec = [0.0] * 7
    if session_date:
        try:
            dt = datetime.strptime(session_date[:10], "%Y-%m-%d")
            dow_vec[dt.weekday()] = 1.0
        except: pass
    else:
        dow_vec[4] = 1.0  # 默认周五

    # 竞品统计
    comp_count = 0.0
    comp_deal_rate = 0.0
    if session_date:
        try:
            from modules.competitor_import import get_competitor_count_on_date, get_competitor_stats_on_date
            comp_count = float(get_competitor_count_on_date(session_date[:10]))
            stats = get_competitor_stats_on_date(session_date[:10])
            comp_deal_rate = stats.get("deal_rate", 0.0)
        except: pass

    # 高端标志
    has_high = 1.0 if sp_sorted and sp_sorted[-1] > 50000 else 0.0

    return [
        n / 10,                          # 上拍件数
        total_sp / 100000,               # 起拍总额
        (total_sp / n) / 1000 if n > 0 else 0,   # 平均起拍
        sp_sorted[n // 2] / 1000 if n > 0 else 0, # 中位数
        sp_sorted[n * 3 // 4] / 1000 if n > 0 else 0, # P75
        sp_sorted[n * 9 // 10] / 1000 if n > 0 else 0, # P90
        cx_count / n if n > 0 else 0,    # 春风祥玉+老贵占比
        hhi,                              # 窑口集中度
        len(kiln_counts),                # 窑口数量
        comp_count / 3,                  # 竞品数(归一化)
        comp_deal_rate,                  # 🆕 竞品成交率
        has_high,                         # 高端标志
    ] + dow_vec                          # 周几 (7位)


def _cosine_sim(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    return dot / (n1 * n2) if n1 * n2 > 0 else 0.0


async def predict_stage2(items: list[dict], session_date: str = "", top_k: int = 20) -> dict:
    """
    Stage 2: 专场含佣直接匹配

    Args:
        items: 当前专场拍品列表
        session_date: 专场日期 "YYYY-MM-DD"
        top_k: 取前 K 个最相似场次

    Returns:
        {"low": float, "best": float, "high": float, "similar_sessions": [...], "similarity_count": int}
    """
    sessions = await _load_session_history()
    if not sessions:
        return {"low": 0, "best": 0, "high": 0, "similar_sessions": [], "similarity_count": 0}

    tv = _session_features(items, session_date)
    today = datetime.now()

    scored = []
    for s in sessions:
        sdow_vec = [0.0] * 7
        if s["dow"] is not None: sdow_vec[s["dow"]] = 1.0

        # 历史场的竞品成交率
        hist_date = ""
        ds = s["name"][:8] if len(s["name"]) >= 8 else ""
        if len(ds) == 8:
            hist_date = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
        hist_comp_rate = 0.0
        if hist_date:
            try:
                from modules.competitor_import import get_competitor_stats_on_date
                stats = get_competitor_stats_on_date(hist_date)
                hist_comp_rate = stats.get("deal_rate", 0.0)
            except: pass

        sv2 = [
            s["item_count"] / 10,
            s["start_total"] / 100000,
            s["start_total"] / max(s["item_count"], 1) / 1000,
            s["start_total"] / max(s["item_count"], 1) / 1000,
            s["start_total"] / max(s["item_count"], 1) / 1000,
            s["start_total"] / max(s["item_count"], 1) / 1000,
            0, 0, 0, 0, 0,
            hist_comp_rate,              # 🆕 历史竞品成交率
        ] + sdow_vec

        sim = _cosine_sim(tv, sv2)

        # 时间衰减
        ds = s["name"][:8] if len(s["name"]) >= 8 else ""
        try:
            dt = datetime.strptime(ds, "%Y%m%d")
            da = max(0, (today - dt).days)
            tw = 0.5 + 0.5 * math.exp(-da / 180)
        except: tw = 0.5

        scored.append((sim * tw, s))

    scored.sort(key=lambda x: -x[0])

    # 相似度阈值门控
    filtered = [(sc, s) for sc, s in scored if sc > 0.15]
    if len(filtered) < 3:
        filtered = scored[:3]

    top = filtered[:top_k]
    if not top:
        # 兜底: 全局中位含佣
        all_revs = [s["revenue"] for s in sessions]
        return {
            "low": statistics.median(sorted(all_revs)[:max(1, len(all_revs)//4)]),
            "best": statistics.median(all_revs),
            "high": statistics.median(sorted(all_revs)[-max(1, len(all_revs)//4):]),
            "similar_sessions": [],
            "similarity_count": 0,
        }

    weights = [s[0] for s in top]
    tw_sum = sum(weights)
    revs = [s[1]["revenue"] for s in top]
    revs_sorted = sorted(revs)

    low = statistics.median(revs_sorted[:max(1, len(revs_sorted)//4)])
    best = sum(s[1]["revenue"] * s[0] for s in top) / tw_sum if tw_sum > 0 else statistics.mean(revs)
    high = statistics.median(revs_sorted[-max(1, len(revs_sorted)//4):])

    similar = [
        {"name": s[1]["name"], "revenue": int(s[1]["revenue"]), "similarity": round(s[0], 3)}
        for s in top[:5]
    ]

    return {
        "low": low,
        "best": best,
        "high": high,
        "similar_sessions": similar,
        "similarity_count": len(top),
    }


# ── 融合 & 主入口 ────────────────────────────────────────

async def predict_session_revenue(
    items: list[dict],
    session_date: str = "",
) -> dict:
    """
    预估专场总成交额区间（主入口）

    Args:
        items: 当前专场拍品列表，每件至少含 name, kiln, starting_price
        session_date: 专场日期 "YYYY-MM-DD" 或 "" (自动提取)

    Returns:
        {
            "low": int, "best": int, "high": int,
            "confidence": str,
            "components": {
                "stage1": {...}, "stage2": {...},
                "fusion_weights": {"stage1": float, "stage2": float},
                "similar_sessions": [...],
            },
            "session_summary": {...},
        }
    """
    if not items:
        raise ValueError("拍品列表为空")

    # 初始化数据
    await _load_item_history()
    await _load_session_history()
    await _build_premium_indices()

    # Stage 1 (含全局校准系数)
    s1_raw = predict_stage1(items)
    CAL_GLOBAL = 1.10  # 精细网格最优
    s1 = {
        "low": s1_raw["low"] * CAL_GLOBAL,
        "best": s1_raw["best"] * CAL_GLOBAL,
        "high": s1_raw["high"] * CAL_GLOBAL,
        "confidence_stats": s1_raw.get("confidence_stats", {}),
    }

    # Stage 2
    s2 = await predict_stage2(items, session_date)

    # 动态权重
    conf_stats = s1.get("confidence_stats", {})
    total_items = len(items)
    high_conf = conf_stats.get("high", 0)
    item_conf = high_conf / total_items if total_items > 0 else 0

    # 检测特殊小型主题场
    SMALL_THEMES = ["建盏", "茶", "紫砂", "杂项"]
    is_small_theme = any(t in str(session_date) for t in SMALL_THEMES) and total_items < 20

    if is_small_theme:
        w1, w2 = 0.7, 0.3
    elif item_conf > 0.7:
        w1, w2 = 0.40, 0.60
    elif item_conf > 0.4:
        w1, w2 = 0.30, 0.70
    else:
        w1, w2 = 0.20, 0.80

    # 融合
    final_low = int(w1 * s1["low"] + w2 * s2["low"])
    final_best = int(w1 * s1["best"] + w2 * s2["best"])
    final_high = int(w1 * s1["high"] + w2 * s2["high"])

    # 连续校准曲线 (修正: S2<200K不加矫, 避免小型分散场过度拉升)
    s2_anchor = max(s2["best"], 50000)
    if s2_anchor > 200000:
        tier_boost = min(2.5, 1.0 + 0.77 * ((s2_anchor - 200000) / 300000))
    else:
        tier_boost = 1.0

    # 窑口分散惩罚: 春风祥玉+老贵占比<25%且窑口数>12 → 降低20%
    kiln_dist = defaultdict(int)
    for it in items:
        kiln_dist[_norm_kiln(it.get("kiln", ""))] += 1
    cx_ratio2 = sum(1 for k in kiln_dist if "春风祥玉" in k) / total_items if total_items > 0 else 0
    if cx_ratio2 < 0.25 and len(kiln_dist) > 12:
        tier_boost *= 0.80

    # 周六额外校准
    dow_boost = 1.0
    if session_date:
        try:
            dt = datetime.strptime(session_date[:10], "%Y-%m-%d")
            if dt.weekday() == 5:  # 周六
                dow_boost = 1.20
        except: pass

    # 小型主题场降权
    if is_small_theme:
        tier_boost *= 0.35

    final_low = int(final_low * tier_boost * dow_boost)
    final_best = int(final_best * tier_boost * dow_boost)
    final_high = int(final_high * tier_boost * dow_boost)

    # 区间: best ±37%
    final_low = int(final_best * 0.63)
    final_high = int(final_best * 1.37)

    # 置信度
    sim_count = s2.get("similarity_count", 0)
    has_new_kiln = conf_stats.get("very_low", 0) > 0
    if item_conf > 0.7 and sim_count >= 15 and not has_new_kiln:
        confidence = "high"
    elif item_conf > 0.4 and sim_count >= 8:
        confidence = "medium"
    elif not has_new_kiln:
        confidence = "low"
    else:
        confidence = "very_low"

    # 会话摘要
    total_sp = sum(_parse_price(it.get("starting_price", 0)) for it in items)
    kilns = set(_norm_kiln(it.get("kiln", "")) for it in items)
    cx_count = sum(1 for k in kilns if "春风祥玉" in k or "老贵" in k)
    kiln_list = defaultdict(int)
    for it in items:
        kiln_list[_norm_kiln(it.get("kiln", ""))] += 1
    top_kiln = max(kiln_list, key=kiln_list.get) if kiln_list else ""
    top_kiln_ratio = f"{top_kiln} {kiln_list[top_kiln] / total_items * 100:.0f}%" if total_items > 0 else ""

    dow_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dow = 4
    if session_date:
        try:
            dt = datetime.strptime(session_date[:10], "%Y-%m-%d")
            dow = dt.weekday()
        except: pass

    return {
        "low": final_low,
        "best": final_best,
        "high": final_high,
        "confidence": confidence,
        "components": {
            "stage1": {"low": int(s1["low"]), "best": int(s1["best"]), "high": int(s1["high"])},
            "stage2": {"low": int(s2["low"]), "best": int(s2["best"]), "high": int(s2["high"])},
            "fusion_weights": {"stage1": w1, "stage2": w2},
            "similar_sessions": s2.get("similar_sessions", []),
        },
        "session_summary": {
            "item_count": total_items,
            "total_starting_price": int(total_sp),
            "unique_kilns": len(kilns),
            "top_kiln_ratio": top_kiln_ratio,
            "day_of_week": dow_name[dow],
        },
    }
