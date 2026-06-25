"""
飞书 Bitable API 集成模块
通过飞书开放 API 直接读取多维表格中的拍品数据
"""
import logging
import os
import re
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
load_dotenv()

import httpx

logger = logging.getLogger(__name__)

# API 凭证（从环境变量读取）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

# 飞书 API 端点
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FIELDS_URL = "https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
RECORDS_URL = "https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

# 飞书字段名 → 标准字段名映射
FIELD_NAME_MAP = {
    "拍品名称": "name",
    "拍品名字": "name",
    "品名": "name",
    "窑口": "kiln",
    "窑口名称": "kiln",
    "款识": "inscription",
    "底款": "inscription",
    "年代": "era",
    "朝代": "era",
    "年份": "era",
    "材质": "material",
    "质地": "material",
    "尺寸": "size",
    "规格": "size",
    "容量": "capacity",
    "起拍价": "starting_price",
    "起拍": "starting_price",
    "参考价": "estimate",
    "估价": "estimate",
    "编号": "code",
    "拍品编号": "code",
    "ID": "code",
    "品相": "condition",
    "描述": "description",
    "拍品描述": "description",
    "原盒/证书": "certificate",
    "标识": "marking",
    "加价幅度": "bid_increment",
    "落槌价": "hammer_price",
    "专场名称": "auction_session",
}


def parse_feishu_url(url: str) -> dict:
    """
    从飞书多维表格 URL 中提取 app_token 和 table_id

    URL 格式:
    https://xxx.feishu.cn/base/{app_token}?table={table_id}&view={view_id}
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # 从路径提取 app_token
    # /base/MbecbVJQ5aVQjBsWvGRcbEg6nMg
    path_parts = parsed.path.strip("/").split("/")
    app_token = None
    if len(path_parts) >= 2 and path_parts[0] == "base":
        app_token = path_parts[1]

    # 从 query 提取 table_id
    table_id = params.get("table", [None])[0]
    view_id = params.get("view", [None])[0]

    return {
        "app_token": app_token,
        "table_id": table_id,
        "view_id": view_id,
    }


# Token 缓存（有效期2小时）
_cached_token: str | None = None
_cached_token_time: float = 0


async def get_tenant_access_token() -> str:
    """获取飞书 tenant_access_token（带缓存）"""
    global _cached_token, _cached_token_time

    import time as _time
    now = _time.time()

    # 缓存有效（提前5分钟刷新）
    if _cached_token and (now - _cached_token_time) < 6900:  # 115分钟
        return _cached_token

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TOKEN_URL,
            json={
                "app_id": FEISHU_APP_ID,
                "app_secret": FEISHU_APP_SECRET,
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书认证失败: {data.get('msg', '未知错误')}")

        _cached_token = data["tenant_access_token"]
        _cached_token_time = now
        logger.info("飞书 Token 已刷新")
        return _cached_token


async def get_table_fields(app_token: str, table_id: str) -> list[dict]:
    """获取表格字段定义"""
    token = await get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            FIELDS_URL.format(app_token=app_token, table_id=table_id),
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取字段失败: {data.get('msg', '未知错误')}")
        return data.get("data", {}).get("items", [])


async def get_table_records(
    app_token: str, table_id: str, page_size: int = 100, page_token: str = None
) -> dict:
    """
    获取表格记录

    Returns:
        {
            "items": [{"fields": {...}, "id": "..."}],
            "total": 35,
            "has_more": false,
            "page_token": "..."
        }
    """
    token = await get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    params = {"page_size": page_size}
    if page_token:
        params["page_token"] = page_token

    last_error = None
    for attempt in range(2):
        try:
            token = await get_tenant_access_token()
            headers = {"Authorization": f"Bearer {token}"}

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    RECORDS_URL.format(app_token=app_token, table_id=table_id),
                    headers=headers,
                    params=params,
                )
                data = resp.json()
                code = data.get("code", -1)

                if code == 0:
                    return data.get("data", {})

                # Token 过期 → 清缓存重试
                msg = data.get("msg", "")
                if "token" in msg.lower() or "access" in msg.lower() or code in (99991663, 99991664, 99991668):
                    global _cached_token
                    _cached_token = None
                    logger.info("Token 已过期，刷新后重试...")
                    if attempt == 0:
                        continue

                raise RuntimeError(f"获取记录失败(code={code}): {msg}")

        except Exception as e:
            last_error = e
            logger.warning(f"API请求失败(尝试{attempt+1}/2): {e}")
            if attempt == 0:
                await __import__('asyncio').sleep(2)

    raise RuntimeError(f"获取记录失败(重试2次后): {last_error}")


async def get_all_records(app_token: str, table_id: str) -> list[dict]:
    """获取全部表格记录（自动翻页）"""
    all_items = []
    page_token = None

    while True:
        data = await get_table_records(app_token, table_id, page_token=page_token)
        items = data.get("items", [])
        all_items.extend(items)

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")

    return all_items


def map_record_to_auction(record: dict) -> dict:
    """
    将飞书表格记录映射为拍品信息字典

    Args:
        record: 飞书 API 返回的单条记录 {"fields": {...}, "id": "..."}

    Returns:
        标准化的拍品信息字典
    """
    fields = record.get("fields", {})

    info = {
        "name": None,
        "code": None,
        "kiln": None,
        "era": None,
        "material": None,
        "size": None,
        "capacity": None,
        "starting_price": None,
        "estimate": None,
        "condition": None,
        "inscription": None,
        "description": None,
        "certificate": None,
        "marking": None,
        "bid_increment": None,
        "hammer_price": None,
        "auction_session": None,
        "record_id": record.get("id", ""),
        "full_text": "",
    }

    for field_name, field_value in fields.items():
        # 映射字段名
        mapped = FIELD_NAME_MAP.get(field_name, field_name.lower())

        # 处理不同类型的字段值
        if isinstance(field_value, list):
            # 数组类型（如微拍堂后台ID）
            if field_value and isinstance(field_value[0], dict):
                field_value = field_value[0].get("text", str(field_value))
            else:
                field_value = ", ".join(str(v) for v in field_value)
        elif isinstance(field_value, (int, float)):
            field_value = str(field_value)

        if mapped in info:
            info[mapped] = str(field_value).strip() if field_value else None

    # 构建全文文本（用于正则兜底和搜索关键词）
    full_parts = []
    for k, v in info.items():
        if v and k not in ("full_text", "record_id"):
            full_parts.append(f"{k}: {v}")
    info["full_text"] = "\n".join(full_parts)

    return info


async def read_feishu_bitable(url: str) -> list[dict]:
    """
    通过飞书 API 读取多维表格中的全部拍品数据

    Args:
        url: 飞书多维表格链接

    Returns:
        拍品信息列表，每条为一个拍品
    """
    logger.info(f"飞书 API 读取: {url}")

    parsed = parse_feishu_url(url)
    app_token = parsed["app_token"]
    table_id = parsed["table_id"]

    if not app_token or not table_id:
        raise ValueError(f"无法从链接中提取 app_token 或 table_id: {url}")

    logger.info(f"app_token: {app_token}, table_id: {table_id}")

    # 获取全部记录
    records = await get_all_records(app_token, table_id)
    logger.info(f"获取到 {len(records)} 条记录")

    # 映射为标准格式
    auction_items = []
    for record in records:
        item = map_record_to_auction(record)
        auction_items.append(item)
        logger.debug(f"  拍品: {item.get('name', '未知')} | 窑口: {item.get('kiln', '未知')}")

    return auction_items
