"""
Obsidian 成交行情搜索模块
从 raw/数据/成交行情.csv 读取拍卖成交记录
搜索规则: 窑口名称 + 拍品关键词匹配

CSV 列: id, goods_name, start_price, deal_price, auction_time, source, is_hide
"""
import csv
import logging
from pathlib import Path
from typing import Optional

import config
from utils.text_parser import extract_vessel, extract_motif, extract_craft

logger = logging.getLogger(__name__)

VAULT_PATH = Path(config.OBSIDIAN_VAULT_PATH)
CSV_PATH = VAULT_PATH / config.OBSIDIAN_MARKET_CSV

# 内存缓存
_csv_cache: Optional[list[dict]] = None


def _load_market_data() -> list[dict]:
    """加载成交行情 CSV 到内存（首次调用时读取，后续命中缓存）"""
    global _csv_cache
    if _csv_cache is not None:
        return _csv_cache

    if not CSV_PATH.exists():
        logger.warning(f"成交行情 CSV 不存在: {CSV_PATH}")
        _csv_cache = []
        return _csv_cache

    records = []
    try:
        with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                is_hide = row.get("is_hide", "").strip()
                status = "隐藏" if is_hide in ("隐藏", "0") else "展示"
                records.append({
                    "id": row.get("id", "").strip(),
                    "name": row.get("goods_name", "").strip(),
                    "starting_price": row.get("start_price", "0").strip(),
                    "deal_price": row.get("deal_price", "0").strip(),
                    "auction_time": row.get("auction_time", "").strip(),
                    "source": row.get("source", "").strip(),
                    "status": status,
                    "image": "",
                    "url": f"obsidian://open?vault={config.OBSIDIAN_VAULT_NAME}&file=raw%2F数据%2F成交行情.csv",
                })
    except Exception as e:
        logger.error(f"读取成交行情 CSV 失败: {e}")
        _csv_cache = []
        return _csv_cache

    _csv_cache = records
    logger.info(f"成交行情加载完成: {len(records)} 条")
    return _csv_cache


def _score_match(record: dict, kiln: str, vessel: str, motif: str, craft: str) -> int:
    """
    三级权重打分：
    - 窑口必须匹配: +100（没匹配直接淘汰，不进入评分）
    - 器型匹配: +30（马蹄杯 > 杯 — 越具体得分越高）
    - 画片/题材匹配: +20
    - 工艺匹配: +10
    - 成交价越高，参考价值越大: +1~3
    """
    name = record.get("name", "")
    score = 0

    # 窑口匹配（已在外部过滤，此处做确认性加分）
    # 精确窑口匹配（如"春风祥玉"）比别名（如"老春"）分值更高
    if kiln and kiln in name:
        score += 100
    else:
        # 别名匹配得分稍低
        score += 80

    # 器型匹配（权重最高）
    if vessel:
        if vessel in name:
            score += 30
        else:
            # 器型可能是"马蹄杯"，CSV里可能是"缸杯"、"卧足杯"等
            # 尝试通用器型匹配
            generic = _to_generic_vessel(vessel)
            if generic and generic in name:
                score += 15

    # 画片/题材匹配
    if motif:
        # 去掉"纹"后缀做模糊匹配
        motif_clean = motif.replace("纹", "")
        if motif in name or motif_clean in name:
            score += 20
        else:
            # 题材可能以不同形式出现，尝试部分匹配
            # 例如 "事事如意纹" → "事事如意"在名称中
            for part in [motif, motif_clean]:
                if len(part) >= 3 and part in name:
                    score += 10
                    break

    # 工艺匹配
    if craft:
        if craft in name:
            score += 10
        else:
            # 工艺模糊匹配：如 "青花暗刻" → "青花"在名称中
            for sub in craft.split("暗刻") if "暗刻" in craft else [craft]:
                if sub and len(sub) >= 2 and sub in name:
                    score += 5
                    break

    # 成交价加权
    try:
        deal = float(record.get("deal_price", "0").replace(",", ""))
        if deal > 50000:
            score += 3
        elif deal > 10000:
            score += 2
        elif deal > 1000:
            score += 1
    except (ValueError, TypeError):
        pass

    return score


def _to_generic_vessel(vessel: str) -> str:
    """将具体器型映射到通用器型，如 马蹄杯→杯, 缸杯→杯, 压手杯→杯"""
    generic_map = {
        "马蹄杯": "杯", "缸杯": "杯", "压手杯": "杯", "撇口杯": "杯",
        "炉式杯": "杯", "高足杯": "杯", "直口杯": "杯", "手雷杯": "杯",
        "玉兰杯": "杯", "鸡心杯": "杯", "斗笠杯": "杯", "铃铛杯": "杯",
        "仰钟杯": "杯", "罗汉杯": "杯", "公道杯": "杯", "卧足杯": "杯",
        "翻足杯": "杯", "鼓型杯": "杯", "敞口杯": "杯", "撇口杯": "杯",
        "冰桶杯": "杯", "若琛杯": "杯", "品杯": "杯",
        "盖碗": "碗", "三才盖碗": "碗", "大碗": "碗", "压手大碗": "碗",
        "壶": "壶", "掇只壶": "壶", "圆珠壶": "壶",
        "盘": "盘", "碟": "碟", "盅": "盅", "洗": "洗",
        "罐": "罐", "瓶": "瓶", "炉": "炉",
    }
    return generic_map.get(vessel, "")


async def search_auction_records(item_name: str, kiln: str) -> list[dict]:
    """
    从成交行情 CSV 搜索拍卖记录

    搜索策略（三级权重）:
    1. 窑口名称必须匹配（goods_name 中包含窑口名或别名）
    2. 器型匹配 +30（器型越具体得分越高）
    3. 画片/题材匹配 +20
    4. 工艺匹配 +10
    5. 最多返回 20 条，优先展示高价值+近期的记录

    Args:
        item_name: 拍品名称
        kiln: 窑口名称

    Returns:
        [{"image": "", "name": "拍品名称", "starting_price": "0",
          "deal_price": "0", "auction_time": "YYYY-MM-DD", "source": "来源",
          "status": "展示/隐藏", "url": "obsidian://链接"}]
    """
    if not kiln:
        return []

    # 提取关键词
    vessel = extract_vessel(item_name)
    motif = extract_motif(item_name)
    craft = extract_craft(item_name)

    logger.info(f"成交行情搜索: 窑口={kiln} 器型={vessel} 画片={motif} 工艺={craft}")

    records = _load_market_data()
    if not records:
        return []

    # 筛选窑口匹配的记录
    scored = []
    for r in records:
        name = r.get("name", "")
        # 窑口必须匹配（硬门槛）
        if kiln not in name and not _kiln_alias_match(kiln, name):
            continue

        s = _score_match(r, kiln, vessel, motif, craft)
        scored.append((s, r))

    # 按得分降序，同分按成交价降序
    scored.sort(key=lambda x: (x[0], float(x[1].get("deal_price", "0").replace(",", "") or "0")), reverse=True)

    results = [r for _, r in scored[:20]]

    logger.info(f"成交行情搜索: {len(results)} 条匹配 (总共 {len(records)} 条)")
    return results


def _kiln_alias_match(kiln: str, name: str) -> bool:
    """
    窑口别名匹配
    例如 "春风祥玉" 匹配名称中的 "春风祥玉"或"老春"
         "贵和祥" 匹配名称中的 "贵和祥"或"老贵"
    """
    aliases = {
        "贵和祥": ["老贵", "贵和祥"],
        "春风祥玉": ["春风祥玉", "老春", "春风"],
        "小雅": ["小雅"],
        "九段烧": ["九段烧", "九段"],
        "克勤堂": ["克勤堂"],
        "快雪时晴": ["快雪时晴", "快雪"],
        "泽善堂": ["泽善堂"],
        "畊雨窑": ["畊雨窑", "畊雨"],
        "铭经草堂": ["铭经草堂", "铭经"],
        "陶人临古": ["陶人临古", "陶人"],
        "觉山隐窑": ["觉山隐窑", "觉山"],
        "青如堂": ["青如堂"],
        "立明堂": ["立明堂"],
        "自牧堂": ["自牧堂"],
    }

    candidates = aliases.get(kiln, [kiln])
    return any(a in name for a in candidates)


def generate_market_analysis(records: list[dict], kiln: str, item_name: str) -> str:
    """
    基于成交记录生成行情分析

    分析维度:
    1. 成交价格区间和均价
    2. 近期成交趋势（近半年 vs 更早）
    3. 来源分布（威拍/小茶书/正观堂）
    4. 结合 Obsidian 鉴赏知识补充背景

    Returns:
        行情分析文本（约 80-120 字）
    """
    if not records:
        return ""

    # === 价格统计 ===
    prices = []
    for r in records:
        try:
            prices.append(float(r.get("deal_price", "0").replace(",", "")))
        except (ValueError, TypeError):
            pass

    if not prices:
        return ""

    avg_price = int(sum(prices) // len(prices))
    max_price = int(max(prices))
    min_price = int(min(prices))

    # === 时间趋势分析 ===
    from datetime import datetime
    recent_prices = []  # 近6个月
    older_prices = []   # 6个月以前
    cutoff = "2025-12"  # approximate 6 months ago from June 2026

    for r in records:
        try:
            p = float(r.get("deal_price", "0").replace(",", ""))
            t = r.get("auction_time", "")
            if t >= cutoff:
                recent_prices.append(p)
            else:
                older_prices.append(p)
        except (ValueError, TypeError):
            pass

    # === 来源分布 ===
    sources = {}
    for r in records:
        s = r.get("source", "未知")
        sources[s] = sources.get(s, 0) + 1

    top_source = max(sources, key=sources.get) if sources else ""
    top_source_count = sources.get(top_source, 0)

    # === 生成分析文本 ===
    parts = []

    # 窑口行情概述
    parts.append(f"{kiln}同品类拍品")

    # 价格区间
    parts.append(f"成交价区间{min_price}至{max_price}元，均价约{avg_price}元。")

    # 趋势
    if recent_prices and older_prices:
        recent_avg = int(sum(recent_prices) // len(recent_prices))
        older_avg = int(sum(older_prices) // len(older_prices))
        if recent_avg > older_avg:
            diff_pct = int((recent_avg - older_avg) / older_avg * 100)
            parts.append(f"近半年成交均价{recent_avg}元，较此前上涨约{diff_pct}%，行情看涨。")
        elif recent_avg < older_avg:
            diff_pct = int((older_avg - recent_avg) / older_avg * 100)
            parts.append(f"近半年成交均价{recent_avg}元，较此前回落约{diff_pct}%。")
        else:
            parts.append(f"近半年价格保持平稳。")

    # 来源
    source_names = {
        "威拍": "威拍（专业拍卖）", "小茶书": "小茶书（社群拍卖）",
        "正观堂葫芦窑": "正观堂葫芦窑", "高古楼转拍": "高古楼转拍",
        "金刚葫芦娃": "金刚葫芦娃",
    }
    source_label = source_names.get(top_source, top_source)
    if top_source:
        parts.append(f"成交主要来自{source_label}，占{top_source_count}条。")

    # 代表性拍品
    if records:
        top_record = max(records, key=lambda r: float(r.get("deal_price", "0").replace(",", "") or "0"))
        top_name = top_record.get("name", "")
        # 简化名称
        if len(top_name) > 25:
            top_name = top_name[:22] + ".."
        parts.append(f"最高成交为「{top_name}」{top_record.get('deal_price', '')}元。")

    analysis = "".join(parts)
    if len(analysis) > 180:
        analysis = analysis[:175] + "。"
    return analysis


def reload_market_data():
    """强制重新加载 CSV 数据（在 CSV 更新后调用）"""
    global _csv_cache
    _csv_cache = None
    return _load_market_data()


def get_market_stats() -> dict:
    """获取成交行情数据统计（供调试用）"""
    records = _load_market_data()
    if not records:
        return {"total": 0}

    sources = {}
    for r in records:
        s = r.get("source", "未知")
        sources[s] = sources.get(s, 0) + 1

    return {
        "total": len(records),
        "sources": sources,
        "csv_path": str(CSV_PATH),
    }
