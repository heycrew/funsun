"""
竞品 Excel 数据增量导入模块

扫描本地竞品 Excel 文件（金刚葫芦娃/七个点转赚/春风颐和），
通过文件指纹（MD5(路径+修改时间+大小)）实现增量导入。
已导入的文件记录在 cache/competitor_registry.json 中。

Excel 格式（22 列）:
    原数据, 封面图, PS封面图, 拍品名称, 窑口, 款识, 年份, 题材,
    器型, 工艺, 起拍价, 加价幅度, 出价次数, 落槌价, 成交状态,
    拍卖时间, 来源, 其他图, 拍品详情, 切片临时链接, 读取视频文案, 原起拍价
"""
import hashlib
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────
def _get_competitor_dirs() -> list[str]:
    """从 config 或环境变量读取竞品目录，处理分号分隔"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config
        dirs_str = config.COMPETITOR_DIRS
    except Exception:
        dirs_str = os.getenv("COMPETITOR_DIRS", "")
    return [d.strip() for d in dirs_str.split(";") if d.strip()]

COMPETITOR_DIRS = _get_competitor_dirs()
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "cache" / "competitor_registry.json"

# ── 内存缓存 ──────────────────────────────────────────
# 导入后的所有竞品记录
_records_cache: Optional[list[dict]] = None

# 按日期索引: date_str -> [records]
_date_index: Optional[dict[str, list[dict]]] = None

# 按来源索引: source_name -> [records]
_source_index: Optional[dict[str, list[dict]]] = None

# 导入统计
_import_stats: dict = {
    "last_refresh": None,
    "total_files": 0,
    "total_records": 0,
    "new_files": 0,
    "new_records": 0,
    "errors": [],
}


# ── 工具函数 ──────────────────────────────────────────

def _parse_price(value) -> float:
    """解析价格值，支持数字和字符串格式"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).replace(",", "").replace("¥", "").replace(" ", "").replace("\n", "").strip()
        # 处理 "2万" 等格式
        if "万" in s:
            s = s.replace("万", "")
            return float(s) * 10000 if s else 0.0
        return float(s) if s else 0.0
    except (ValueError, TypeError):
        return 0.0


def _compute_fingerprint(filepath: str) -> str:
    """计算文件指纹: MD5(相对路径 + 修改时间 + 文件大小)"""
    try:
        stat = os.stat(filepath)
        raw = f"{filepath}|{stat.st_mtime}|{stat.st_size}"
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return ""


def _load_registry() -> dict:
    """加载已导入文件的指纹注册表"""
    if REGISTRY_PATH.exists():
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"指纹注册表损坏，重建: {e}")
    return {}


def _save_registry(registry: dict):
    """保存指纹注册表"""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _parse_excel(filepath: str) -> list[dict]:
    """解析单个竞品 Excel 文件，返回记录列表"""
    records = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, data_only=True)
        ws = wb.active

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 16:
                continue

            name = str(row[3]) if row[3] else ""
            kiln = str(row[4]) if row[4] else ""
            auction_time = str(row[15]) if row[15] else ""

            # 提取日期
            date_str = ""
            if auction_time and len(auction_time) >= 10:
                date_str = auction_time[:10]

            start_price = _parse_price(row[10])
            hammer_price = _parse_price(row[13])
            status_raw = str(row[14]) if row[14] else ""
            source = str(row[16]) if row[16] else ""

            # 成交状态判定
            is_deal = "成交" in status_raw and "未" not in status_raw
            is_nosale = "流拍" in status_raw or "未支付" in status_raw

            records.append({
                "name": name.strip(),
                "kiln": kiln.strip(),
                "inscription": str(row[5]) if row[5] else "",
                "motif": str(row[7]) if row[7] else "",
                "vessel": str(row[8]) if row[8] else "",
                "craft": str(row[9]) if row[9] else "",
                "start_price": start_price,
                "hammer_price": hammer_price,
                "bid_count": _parse_price(row[12]),
                "status": status_raw.strip(),
                "is_deal": is_deal,
                "is_nosale": is_nosale,
                "auction_time": date_str,
                "source": source.strip(),
            })

        wb.close()
    except Exception as e:
        logger.error(f"解析 Excel 失败: {filepath} - {e}")
        raise

    return records


# ── 公开 API ──────────────────────────────────────────

def refresh(force: bool = False) -> dict:
    """
    扫描竞品文件夹，增量导入新文件。

    Args:
        force: True = 强制全部重新导入

    Returns:
        {"new_files": N, "new_records": N, "total_files": N, "total_records": N, "elapsed": seconds}
    """
    global _records_cache, _date_index, _source_index, _import_stats

    start = time.time()
    registry = {} if force else _load_registry()
    new_files = 0
    new_records = 0
    all_records = list(_records_cache or []) if not force else []
    known_fingerprints = set(registry.values())
    errors = []

    for folder in COMPETITOR_DIRS:
        folder_path = Path(folder)
        if not folder_path.exists():
            logger.warning(f"竞品文件夹不存在: {folder}")
            continue

        # 遍历文件夹及其子文件夹
        xlsx_files = []
        for root, dirs, files in os.walk(folder_path):
            for f in files:
                if f.endswith(".xlsx") and not f.startswith("~$"):
                    xlsx_files.append(os.path.join(root, f))

        for fpath in xlsx_files:
            fp = _compute_fingerprint(fpath)
            if not fp:
                continue

            # 已导入则跳过
            if fp in known_fingerprints and not force:
                continue

            try:
                recs = _parse_excel(fpath)
                all_records.extend(recs)
                registry[fpath] = fp
                known_fingerprints.add(fp)
                new_files += 1
                new_records += len(recs)
            except Exception as e:
                errors.append(str(e))
                continue

    # 更新内存缓存
    _records_cache = all_records
    _build_indices()

    # 保存注册表
    if new_files > 0 or force:
        _save_registry(registry)

    elapsed = time.time() - start
    _import_stats = {
        "last_refresh": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_files": len(registry),
        "total_records": len(all_records),
        "new_files": new_files,
        "new_records": new_records,
        "elapsed": round(elapsed, 1),
        "errors": errors,
    }

    logger.info(f"竞品数据刷新完成: +{new_files} 文件, +{new_records} 条 (耗时 {elapsed:.1f}s)")
    return _import_stats


def _build_indices():
    """重建日期索引和来源索引"""
    global _date_index, _source_index, _records_cache
    _date_index = defaultdict(list)
    _source_index = defaultdict(list)

    if not _records_cache:
        return

    for rec in _records_cache:
        if rec["auction_time"]:
            _date_index[rec["auction_time"]].append(rec)
        _source_index[rec["source"]].append(rec)


def get_all_records() -> list[dict]:
    """获取所有竞品记录（从内存缓存）"""
    global _records_cache
    if _records_cache is None:
        refresh()
    return _records_cache or []


def get_competitors_on_date(date_str: str) -> set[str]:
    """查询某天有哪些竞争对手在线"""
    global _date_index
    if _date_index is None:
        refresh()
    sources = set()
    for rec in (_date_index or {}).get(date_str, []):
        if rec["source"]:
            sources.add(rec["source"])
    return sources


def get_competitor_count_on_date(date_str: str) -> int:
    """查询某天竞争对手数量"""
    return len(get_competitors_on_date(date_str))


def get_competitor_stats_on_date(date_str: str) -> dict:
    """查询某天竞争对手的成交率统计（数据未加载时返回空）"""
    global _date_index
    if _date_index is None:
        return {"total": 0, "deals": 0, "nosale": 0, "deal_rate": 0.0}
    records = (_date_index or {}).get(date_str, [])
    total = len(records)
    if total == 0:
        return {"total": 0, "deals": 0, "nosale": 0, "deal_rate": 0.0}
    deals = sum(1 for r in records if r.get("is_deal"))
    nosale = sum(1 for r in records if r.get("is_nosale"))
    return {
        "total": total,
        "deals": deals,
        "nosale": nosale,
        "deal_rate": round(deals / max(total, 1), 3),
    }


def get_status() -> dict:
    """获取导入状态（供管理后台查询）"""
    global _import_stats
    if _records_cache is None:
        return {"loaded": False, **({} if not _import_stats else _import_stats)}
    return {
        "loaded": True,
        **_import_stats,
    }


def reload():
    """强制重新导入所有竞品数据"""
    global _records_cache, _date_index, _source_index
    _records_cache = None
    _date_index = None
    _source_index = None
    return refresh(force=True)
