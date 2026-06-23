"""
飞书页面读取器
负责打开飞书链接，在 DOM 中定位拍品字段并提取结构化信息
"""
import asyncio
import logging
from urllib.parse import urlparse

from playwright.async_api import Page

from utils.browser import browser_manager
from utils.text_parser import extract_auction_info, extract_kiln_from_dom_text, extract_name_from_dom_text

logger = logging.getLogger(__name__)

FEISHU_DOMAINS = [
    "feishu.cn",
    "larkoffice.com",
    "larksuite.com",
    "bytedance.net",
]

# 字段标签 → 标准化字段名映射
# 覆盖飞书文档中可能出现的各种标签写法
FIELD_LABEL_MAP = {
    # 拍品名称的各种写法
    "拍品名字": "name",
    "拍品名": "name",
    "拍品名称": "name",
    "品名": "name",
    "名称": "name",
    "商品名称": "name",
    # 窑口的各种写法
    "窑口": "kiln",
    "窑口名称": "kiln",
    "窑场": "kiln",
    "所属窑口": "kiln",
    "窑口类型": "kiln",
    # 款识
    "款识": "inscription",
    "底款": "inscription",
    # 编号
    "编号": "code",
    "拍品编号": "code",
    "拍品号": "code",
    "Lot": "code",
    "序号": "code",
    # 尺寸
    "尺寸": "size",
    "规格": "size",
    "大小": "size",
    "容量": "capacity",
    "容积": "capacity",
    # 起拍价
    "起拍价": "starting_price",
    "起拍": "starting_price",
    "起拍价格": "starting_price",
    "起价": "starting_price",
    # 估价
    "估价": "estimate",
    "参考价": "estimate",
    "预估价": "estimate",
    "市场估价": "estimate",
    # 年代
    "年代": "era",
    "朝代": "era",
    "时期": "era",
    "年份": "era",
    # 材质
    "材质": "material",
    "质地": "material",
    "胎质": "material",
    # 描述
    "拍品描述": "description",
    "描述": "description",
    "详情": "description",
    "说明": "description",
}


def is_feishu_url(url: str) -> bool:
    """判断是否为飞书链接"""
    try:
        parsed = urlparse(url)
        return any(domain in parsed.netloc for domain in FEISHU_DOMAINS)
    except Exception:
        return False


def classify_feishu_url(url: str) -> str:
    """分类飞书链接类型"""
    if "bitable" in url or "base" in url:
        return "bitable"
    elif "doc" in url or "docx" in url:
        return "doc"
    elif "share" in url:
        return "shared"
    else:
        return "unknown"


async def _extract_field_from_dom(page: Page, field_label: str) -> str | None:
    """
    在 DOM 中查找字段标签，提取对应的值

    飞书文档的常见 DOM 结构:
    1. 表格单元格: <td>标签</td><td>值</td>
    2. 行内标签: <span>标签：</span><span>值</span> 或文本内 "标签：值"
    3. 块级字段: <div>标签</div><div>值</div>

    策略: 找到包含标签文本的元素，然后找相邻元素或同行文本中的值
    """
    try:
        # 策略1: 在页面上找包含此标签文本的所有元素
        elements = page.locator(f"text={field_label}")

        count = await elements.count()
        if count == 0:
            return None

        # 取第一个匹配
        for i in range(min(count, 3)):  # 最多检查前3个匹配
            el = elements.nth(i)

            # 获取该元素的完整文本（可能包含标签+值）
            full_text = (await el.inner_text()).strip()

            # 如果元素文本比标签长，可能值在同一元素内
            # 例如 "拍品名字：青花瓷瓶" 或 "拍品名字 青花瓷瓶"
            if len(full_text) > len(field_label) + 1:
                # 尝试分隔符拆分
                import re
                # 匹配 标签[：:：\s]+值
                pattern = re.escape(field_label) + r'[：:：\s]+(.+)'
                m = re.search(pattern, full_text)
                if m:
                    value = m.group(1).strip()
                    if value and len(value) >= 2:
                        return value

                # 尝试直接去掉标签前缀
                value = full_text.replace(field_label, "", 1).strip()
                # 去掉开头的冒号等分隔符
                value = re.sub(r'^[：:：\s]+', '', value)
                if value and len(value) >= 2:
                    return value

            # 策略2: 值在下一个兄弟元素中
            try:
                # 尝试找紧跟的 span/div/td
                next_el = page.locator(f"text={field_label} + span, text={field_label} + div")
                next_count = await next_el.count()
                if next_count > 0:
                    next_text = (await next_el.first.inner_text()).strip()
                    if next_text:
                        return next_text
            except Exception:
                pass

            # 策略3: 在父元素中查找（表格场景: 标签在左边td，值在右边td）
            try:
                parent = el.locator("..")
                parent_tag = await parent.evaluate("el => el.tagName")
                if parent_tag and parent_tag.upper() in ("TD", "TH"):
                    # 在表格中，找同一行的下一个单元格
                    row = parent.locator("..")
                    cells = row.locator("td, th")
                    cell_count = await cells.count()
                    for ci in range(cell_count):
                        cell_text = (await cells.nth(ci).inner_text()).strip()
                        if field_label in cell_text and ci + 1 < cell_count:
                            next_cell = await cells.nth(ci + 1).inner_text()
                            next_cell = next_cell.strip()
                            if next_cell and next_cell != field_label:
                                return next_cell
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"DOM提取 '{field_label}' 时出错: {e}")

    return None


async def _extract_all_fields_from_dom(page: Page) -> dict:
    """
    遍历所有已知字段标签，从 DOM 中提取字段值
    返回 {标准化字段名: 值}
    """
    result = {}

    # 按优先级排序：最重要的字段先提取
    priority_labels = [
        "拍品名字", "拍品名", "拍品名称", "品名", "名称",
        "窑口", "窑口名称", "窑场", "所属窑口",
        "编号", "拍品编号", "拍品号", "Lot",
        "年代", "朝代", "时期",
        "材质", "质地",
        "尺寸", "规格",
        "起拍价", "起拍", "起拍价格",
        "估价", "参考价", "预估价",
        "拍品描述", "描述", "详情",
    ]

    for label in priority_labels:
        field_key = FIELD_LABEL_MAP.get(label)
        if not field_key or field_key in result:
            # 该字段已经提取到了，跳过
            continue

        try:
            value = await _extract_field_from_dom(page, label)
            if value and value.strip():
                result[field_key] = value.strip()
                logger.info(f"  DOM提取: {label} → {field_key} = {value.strip()[:50]}")
        except Exception as e:
            logger.debug(f"  提取 '{label}' 失败: {e}")

    return result


async def _extract_table_data(page: Page) -> dict:
    """
    专门处理飞书多维表格/表格形式的拍品数据
    查找页面中的表格，解析为键值对
    """
    result = {}
    try:
        tables = page.locator("table, .table-wrapper table, [class*='table']")
        table_count = await tables.count()

        for ti in range(min(table_count, 5)):
            table = tables.nth(ti)
            rows = table.locator("tr")
            row_count = await rows.count()

            for ri in range(min(row_count, 50)):
                row = rows.nth(ri)
                cells = row.locator("td, th")
                cell_count = await cells.count()

                if cell_count >= 2:
                    # 取同行前两个单元格作为 标签-值 对
                    label_text = (await cells.nth(0).inner_text()).strip()
                    value_text = (await cells.nth(1).inner_text()).strip()

                    # 检查标签是否匹配已知字段
                    for label_pattern, field_key in FIELD_LABEL_MAP.items():
                        if label_pattern in label_text or label_text == label_pattern:
                            if value_text and field_key not in result:
                                result[field_key] = value_text
                                logger.info(f"  表格提取: {label_text} → {field_key} = {value_text[:50]}")
                                break

    except Exception as e:
        logger.debug(f"表格提取失败: {e}")

    return result


async def read_feishu_page(url: str) -> dict:
    """
    读取飞书页面并提取拍品信息

    提取策略（按优先级）：
    1. DOM 结构提取 — 在 HTML 中按字段标签定位值
    2. 表格解析 — 处理飞书多维表格
    3. 全文文本提取 — 正则匹配（兜底）

    Args:
        url: 飞书页面链接

    Returns:
        dict: 拍品结构化信息
    """
    logger.info(f"开始读取飞书页面: {url}")

    if not is_feishu_url(url):
        raise ValueError(f"不是有效的飞书链接: {url}")

    page_type = classify_feishu_url(url)
    logger.info(f"飞书链接类型: {page_type}")

    try:
        async with browser_manager.new_page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等待飞书文档内容渲染完成
            # 飞书文档的常见渲染容器
            try:
                await page.wait_for_selector(
                    ".block-content, .doc-content, .page-content, .bitable-content, "
                    "[class*='content'], [class*='block'], table",
                    timeout=10000,
                )
            except Exception:
                logger.debug("未检测到飞书特定容器，继续提取...")

            # 额外等待确保 JS 渲染完成
            await asyncio.sleep(3)

            # ========== 策略1: DOM 结构提取 ==========
            logger.info("策略1: DOM 结构提取...")
            dom_result = await _extract_all_fields_from_dom(page)

            # ========== 策略2: 表格提取 ==========
            logger.info("策略2: 表格数据提取...")
            table_result = await _extract_table_data(page)

            # 合并结果（DOM 提取优先）
            auction_info = {
                "name": None,
                "code": None,
                "kiln": None,
                "description": None,
                "size": None,
                "weight": None,
                "starting_price": None,
                "estimate": None,
                "era": None,
                "material": None,
                "condition": None,
                "full_text": "",
            }
            auction_info.update(table_result)
            auction_info.update(dom_result)

            # ========== 策略3: 全文文本提取（兜底） ==========
            # 获取页面标题
            title = await page.title()

            # 获取 body 全文
            body_text = await page.inner_text("body")
            auction_info["full_text"] = body_text

            # 对 DOM 未提取到的字段，用正则从全文补充
            regex_result = extract_auction_info(body_text)

            for key in auction_info:
                if not auction_info[key] and regex_result.get(key):
                    auction_info[key] = regex_result[key]

            # 如果名称仍为空，用页面标题
            if not auction_info.get("name") and title:
                auction_info["name"] = title.strip()

            # ========== 日志输出 ==========
            logger.info(f"拍品名称: {auction_info.get('name', '未识别')}")
            logger.info(f"窑口: {auction_info.get('kiln', '未识别')}")
            logger.info(f"年代: {auction_info.get('era', '未识别')}")
            logger.info(f"材质: {auction_info.get('material', '未识别')}")
            logger.info(f"起拍价: {auction_info.get('starting_price', '未识别')}")

            return auction_info

    except Exception as e:
        logger.error(f"读取飞书页面失败: {e}")
        raise RuntimeError(f"读取飞书页面失败: {e}")
