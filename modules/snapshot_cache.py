"""
分析结果快照缓存
- 指纹对比判断是否需要重新分析
- 全量结果持久化到 snapshot.json
- 音频文件按 index 存储（每次覆盖最新）
- 文件锁防止多用户并发
"""
import hashlib, json, logging, os, shutil, time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "cache"
SNAPSHOT_FILE = CACHE_DIR / "snapshot.json"
AUDIO_DIR = CACHE_DIR / "audio"
LOCK_FILE = CACHE_DIR / "analysis.lock"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def item_fingerprint(item: dict) -> str:
    """单条拍品的指纹：code:name:kiln:starting_price:estimate"""
    payload = f"{item.get('code','')}:{item.get('name','')}:{item.get('kiln','')}:{item.get('starting_price','')}:{item.get('estimate','')}"
    return hashlib.md5(payload.encode()).hexdigest()


def compute_fingerprint(items: list[dict]) -> str:
    """根据拍品 code+name 列表计算指纹（MD5）—— 用于全局快速判断"""
    # 按 code 排序确保顺序一致
    sorted_items = sorted(items, key=lambda it: str(it.get('code', '')))
    payload = "|".join(f"{it.get('code','')}:{it.get('name','')}:{it.get('kiln','')}:{it.get('starting_price','')}:{it.get('estimate','')}" for it in sorted_items)
    return hashlib.md5(payload.encode()).hexdigest()


def load_snapshot() -> dict | None:
    """读取缓存的快照"""
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"快照加载: {data.get('total',0)} 条")
        return data
    except Exception as e:
        logger.warning(f"快照读取失败: {e}")
        return None


def save_snapshot(results: list[dict], fingerprint: str):
    """保存全量结果到快照"""
    data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fingerprint,
        "total": len(results),
        "items": results,
    }
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"快照保存: {len(results)} 条")


def build_code_index(snapshot: dict) -> dict[str, dict]:
    """将 snapshot 的 items 列表转为 code→item 索引"""
    idx = {}
    if snapshot and "items" in snapshot:
        for item in snapshot["items"]:
            code = item.get("code", "")
            if code:
                idx[code] = item
    return idx


def delete_audio_for_code(code: str):
    """删除指定 code 对应的音频文件"""
    # code 对应的 index 可能不同，需要从 snapshot 中查找
    snap = load_snapshot()
    if not snap:
        return
    for item in snap.get("items", []):
        if item.get("code") == code:
            idx = item.get("index", -1)
            for suffix in ["", "_tpl"]:
                p = AUDIO_DIR / f"{idx}{suffix}.mp3"
                if p.exists():
                    p.unlink()
            break


def update_market_covers(covers: dict[str, str]):
    """批量更新行情记录封面图 URL 到快照"""
    snap = load_snapshot()
    if not snap or "items" not in snap:
        return
    changed = False
    for item in snap["items"]:
        for rec in item.get("xiaochashu_records", []):
            rid = rec.get("id", "")
            if rid in covers and covers[rid]:
                rec["_cover_image"] = covers[rid]
                changed = True
    if changed:
        save_snapshot(snap["items"], snap.get("fingerprint", ""))
        logger.info(f"封面图缓存已更新: {len(covers)} 条")


def clear_all_audio() -> int:
    """清空全部音频文件，返回删除数量"""
    count = 0
    for f in AUDIO_DIR.glob("*.mp3"):
        f.unlink()
        count += 1
    logger.info(f"已清空 {count} 个音频文件")
    return count


def save_audio(index: int, audio_bytes: bytes, audio_type: str = "commentary"):
    """保存单条音频（覆盖旧文件），audio_type: commentary 或 template"""
    suffix = "_tpl" if audio_type == "template" else ""
    path = AUDIO_DIR / f"{index}{suffix}.mp3"
    path.write_bytes(audio_bytes)


def get_audio(index: int, audio_type: str = "commentary") -> bytes | None:
    """读取缓存音频"""
    suffix = "_tpl" if audio_type == "template" else ""
    path = AUDIO_DIR / f"{index}{suffix}.mp3"
    if path.exists():
        return path.read_bytes()
    return None


def has_audio(index: int, audio_type: str = "commentary") -> bool:
    suffix = "_tpl" if audio_type == "template" else ""
    return (AUDIO_DIR / f"{index}{suffix}.mp3").exists()


def acquire_lock() -> bool:
    """获取分析锁，返回是否成功"""
    if LOCK_FILE.exists():
        # 检查锁是否过期（超过 5 分钟自动释放）
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age > 300:
            logger.warning(f"锁过期 ({age:.0f}s)，强制释放")
            release_lock()
            LOCK_FILE.write_text(str(os.getpid()))
            return True
        return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """释放分析锁"""
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


def update_item_in_snapshot(index: int, commentary: str = None, market_analysis: str = None, template_text: str = None):
    """更新快照中单条拍品的数据"""
    snap = load_snapshot()
    if not snap or "items" not in snap:
        return
    for item in snap["items"]:
        if item.get("index") == index:
            if commentary is not None:
                item["commentary"] = commentary
            if market_analysis is not None:
                item["market_analysis"] = market_analysis
            if template_text is not None:
                item["template_text"] = template_text
            break
    save_snapshot(snap["items"], snap.get("fingerprint", ""))
    logger.info(f"快照已更新: index={index}")


# ── 预估缓存 ──────────────────────────────────────────

PREDICTION_FILE = CACHE_DIR / "prediction_cache.json"


def save_prediction(items: list[dict], result: dict):
    """保存预估结果到缓存（按拍品指纹索引）"""
    fp = compute_fingerprint(items)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "fingerprint": fp,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "result": {k: result[k] for k in ["low", "best", "high", "confidence", "components", "session_summary"]},
    }
    with open(PREDICTION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"预估已缓存: fp={fp[:12]}...")


def get_cached_prediction(items: list[dict]) -> dict | None:
    """如果拍品指纹未变，返回缓存的预估结果"""
    if not PREDICTION_FILE.exists():
        return None
    try:
        with open(PREDICTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        current_fp = compute_fingerprint(items)
        if data.get("fingerprint") == current_fp:
            logger.info(f"命中预估缓存: {data.get('timestamp')}")
            return data.get("result")
    except (json.JSONDecodeError, IOError):
        pass
    return None
