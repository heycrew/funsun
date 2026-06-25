"""
AI 解说稿生成模块 — 基于 DeepSeek API
方案2: 用 vault index.md 导航 → 精准读取 wiki 页面 → 全文上下文发给 LLM。
"""
import re
import logging
from pathlib import Path
from urllib.parse import quote

import httpx

import config
from modules.obsidian_market import search_auction_records, generate_market_analysis

logger = logging.getLogger(__name__)

VAULT = Path(config.OBSIDIAN_VAULT_PATH)
INDEX_FILE = VAULT / "index.md"
WIKI_DIR = VAULT / "wiki"

SYSTEM_PROMPT = """你是一位资深的景德镇瓷器拍卖直播间主播。你需要根据提供的拍品信息、窑口知识和成交行情，生成一段自然流畅、有感染力的拍卖解说稿。

要求：
1. 250-400字，口语化，像在镜头前对藏友说话
2. 开场问候+拍品全名，然后依次自然融入：窑口背景故事→年代→材质→尺寸→品相→画片/工艺亮点→市场数据→竞价引导
3. 必须包含拍品信息中的【全部字段】：年代、材质、尺寸、品相，一个都不能漏。但不要生硬罗列，要像讲故事般自然融入
4. 窑口背景要说最有记忆点的内容（创始人/历史转折/江湖地位）
5. 画片描述要有画面感，不要只说名词
6. 市场价格要转化为说服力——起拍价低就强调性价比，成交价高就强调升值空间
7. 结尾制造紧迫感但不要太夸张，像朋友推荐
8. 纯文本输出，不要用任何 markdown 格式，不要前缀标签，不要换行（整段输出）"""


def _parse_vault_index() -> dict[str, list[tuple[str, str, str]]]:
    """
    解析 vault index.md，返回按类别组织的页面索引。

    Returns:
        { "窑口": [("春风祥玉", "wiki/窑口/春风祥玉.md", "摘要..."), ...],
          "题材": [("婴戏", "wiki/题材/婴戏.md", "摘要..."), ...], ... }
    """
    if not INDEX_FILE.exists():
        return {}

    text = INDEX_FILE.read_text(encoding="utf-8")
    # 去掉 frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        text = text[end + 3:] if end != -1 else text

    catalog = {}
    current_category = None

    for line in text.split("\n"):
        # 检测 ## 分类标题
        m = re.match(r'^##\s+(.+)$', line)
        if m:
            title = m.group(1).strip()
            # 提取中文类别名（去掉 emoji）
            current_category = re.sub(r'[^一-鿿]', '', title)
            if current_category and current_category not in catalog:
                catalog[current_category] = []
            continue

        # 检测 wiki 链接行：- [[页面名]] — 摘要
        links = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', line)
        if links and current_category:
            page_name = links[0]
            # 提取摘要（ — 之后的内容）
            summary = ""
            m2 = re.search(r'[—–-]\s*(.+)$', line)
            if m2:
                summary = m2.group(1).strip()

            # 推断文件路径
            file_path = _resolve_wiki_path(page_name, current_category)
            catalog[current_category].append((page_name, file_path, summary))

    return catalog


def _resolve_wiki_path(page_name: str, category: str) -> str:
    """根据页面名和类别解析实际文件路径"""
    # 类别 → wiki 子目录映射
    cat_dir = {
        "窑口": "窑口", "题材": "题材", "工艺": "工艺",
        "器型": "器型", "器型与茶具": "器型",
        "釉色": "釉色", "匠人": "匠人",
        "品相": "品相", "鉴赏": "鉴赏", "文化": "文化",
        "对比": "对比",
    }
    subdir = cat_dir.get(category, category)
    # 尝试多个可能的路径
    candidates = [
        WIKI_DIR / subdir / f"{page_name}.md",
        VAULT / f"{page_name}.md",  # 根目录
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # 返回推测路径
    return str(WIKI_DIR / subdir / f"{page_name}.md")


def _read_page(file_path: str) -> str:
    """读取 wiki 页面的正文（去掉 frontmatter）"""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---"):
        end = text.find("---", 3)
        text = text[end + 3:] if end != -1 else text
    # 去掉 wiki 链接语法但保留文本
    text = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', text)
    # 去掉 markdown 格式字符
    text = re.sub(r'[*_>`#]', '', text)
    return text.strip()


def _gather_obsidian_context(item: dict) -> str:
    """
    用 vault index.md 导航，精准收集上下文。

    1. 解析 index.md → 获取全站页面索引
    2. 匹配窑口名 → 读取完整窑口页面
    3. 匹配画片/题材 → 读取题材页面
    4. 匹配工艺 → 读取工艺页面
    5. 匹配器型 → 读取器型页面
    """
    from utils.text_parser import extract_vessel, extract_motif, extract_craft

    name = item.get("name", "")
    kiln = item.get("kiln", "")
    vessel = extract_vessel(name)
    motif = extract_motif(name)
    craft = extract_craft(name)

    catalog = _parse_vault_index()
    parts = []

    # === 窑口页面 ===
    kiln_pages = catalog.get("窑口", [])
    kiln_file = None
    for page_name, path, summary in kiln_pages:
        if kiln in page_name or page_name in kiln:
            kiln_file = path
            parts.append(f"## {page_name}（窑口）\n{_read_page(path)}\n")
            break
    if not kiln_file and kiln:
        # 直接尝试文件路径
        direct = WIKI_DIR / "窑口" / f"{kiln}.md"
        if direct.exists():
            parts.append(f"## {kiln}（窑口）\n{_read_page(str(direct))}\n")

    # === 题材页面 ===
    motif_pages = catalog.get("题材", [])
    for page_name, path, summary in motif_pages:
        if motif and (motif.replace("纹", "") in page_name or page_name in motif):
            parts.append(f"## {page_name}（题材/画片）\n{_read_page(path)}\n")
            break

    # === 工艺页面 ===
    craft_pages = catalog.get("工艺", [])
    for page_name, path, summary in craft_pages:
        if craft and any(kw in page_name for kw in craft.split("暗刻") if kw):
            parts.append(f"## {page_name}（工艺）\n{_read_page(path)}\n")
            break

    # === 器型页面 ===
    vessel_pages = catalog.get("器型", []) + catalog.get("器型与茶具", [])
    for page_name, path, summary in vessel_pages:
        if vessel and (vessel in page_name or page_name in vessel or _vessel_generic_match(vessel, page_name, _read_page(path))):
            parts.append(f"## {page_name}（器型）\n{_read_page(path)}\n")
            break

    # === 釉色页面（如果工艺涉及釉色） ===
    glaze_pages = catalog.get("釉色", [])
    if craft:
        for page_name, path, summary in glaze_pages:
            content = _read_page(path)
            if craft_part_matches_glaze(craft, page_name, content):
                parts.append(f"## {page_name}（釉色）\n{content}\n")
                break

    return "\n".join(parts)


def _vessel_generic_match(vessel: str, page_name: str, content: str) -> bool:
    """器型通用匹配：马蹄杯→杯，检查页面是否提及该通用器型"""
    generic = {
        "马蹄杯": "杯", "缸杯": "杯", "压手杯": "杯", "斗笠杯": "杯",
        "盖碗": "碗", "公道杯": "杯",
    }.get(vessel, vessel)
    return generic in page_name or generic in content[:500]


def craft_part_matches_glaze(craft: str, page_name: str, content: str) -> bool:
    """检查工艺关键词是否涉及釉色"""
    parts = craft.replace("暗刻", " ").split()
    for p in parts:
        if len(p) >= 2 and (p in page_name or p in content[:500]):
            return True
    return False


async def generate_commentary_ai_direct(item: dict, obsidian_context: str, market_records: list, market_analysis: str, index: int = 0) -> str:
    """纯 AI 调用版 — 固定模板 + AI 查询结果 = 解说稿"""
    name = item.get("name", "")
    kiln = item.get("kiln", "")
    cfg = _load_commentary_config()
    template = cfg.get("commentary_template", "")

    # 固定模板部分
    fixed_part = _render_template(template, item, index)

    # AI 查询部分
    user_parts = _build_prompt(item, name, kiln, obsidian_context, market_records, market_analysis, fixed_part, index)
    user_prompt = "\n".join(user_parts)
    logger.info(f"Prompt长度: {len(user_prompt)} 字")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": 0.8,
                "max_tokens": 1200,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        ai_result = data["choices"][0]["message"]["content"].strip()
        logger.info(f"AI 解说稿生成成功: {len(ai_result)} 字")

    # AI 结果已包含固定模板上下文
    return ai_result


def _load_commentary_config() -> dict:
    """加载解说稿配置"""
    import json
    from pathlib import Path
    cf = Path(__file__).parent.parent / "data" / "config.json"
    if cf.exists():
        return json.loads(cf.read_text(encoding="utf-8"))
    return {}


def _render_template(template: str, item: dict, index: int) -> str:
    """用拍品数据渲染固定模板，如 [拍品名字] → item['name']
    规则：若字段值为空或'无'，则移除该字段对应的描述短语（含前导标点）"""
    import re
    field_map = {
        "序号": str(index + 1), "拍品名字": item.get("name", ""),
        "窑口": item.get("kiln", ""), "款识": item.get("inscription", item.get("kiln", "")),
        "年份": item.get("era", ""), "年代": item.get("era", ""),
        "尺寸": item.get("size", ""), "容量": item.get("capacity", item.get("size", "")),
        "品相": item.get("condition", ""), "参考价": item.get("estimate", ""),
        "起拍价": item.get("starting_price", ""), "材质": item.get("material", ""),
        "市场参考价": item.get("estimate", ""),
    }
    # 特殊模板单元：整句「本件拍品[原盒/证书]」根据飞书"原盒/证书"字段值替换
    cert_val = item.get("certificate", "") or ""
    cert_replacement = ""
    if cert_val == "有/无":
        cert_replacement = "本件拍品有窑口原配盒子"
    elif cert_val == "有/有":
        cert_replacement = "本件拍品有证书有窑口原配盒子"
    elif cert_val == "无/有":
        cert_replacement = "本件拍品有证书"
    # 无/无 或空值 → cert_replacement 保持 ""（整句移除）
    template = template.replace("本件拍品[原盒/证书]", cert_replacement)

    def repl(m):
        key = m.group(1)
        val = field_map.get(key, m.group(0))
        return val if val else m.group(0)  # 空值保留占位符，后续清理

    result = re.sub(r'\[([^\]]+)\]', repl, template)

    # 清理规则：移除 空值占位符 或 值为"无"的字段
    # 1. 移除 [任意占位符]（值为空未被替换的）
    result = re.sub(r'\[[^\]]+\]', '', result)
    # 2. 移除 "无" 值及其前面的描述文字/标点
    #    匹配模式：标点+任意非标点字符+无，如 "，容量无" → 删除
    result = re.sub(r'[，、；]\s*[^，。；！？、]*?无', '', result)
    # 3. 清理残留：连续的标点 → 单个
    result = re.sub(r'[，、；]{2,}', '，', result)
    # 4. 清理句首逗号
    result = re.sub(r'^[，、；]\s*', '', result)
    # 5. 清理句尾多余的标点后跟句号
    result = re.sub(r'[，、；]\s*。', '。', result)

    return result


def _build_prompt(item, name, kiln, obsidian_context, market_records, market_analysis, fixed_part="", index=0):
    """组装 DeepSeek prompt — 每项参数独立一行 + Obsidian上下文"""
    cfg = _load_commentary_config()
    ai_prompt = cfg.get("ai_query_prompt", "请根据拍品信息生成专业的拍卖解说词")
    ai_prompt = _render_template(ai_prompt, item, index)

    user_parts = [
        ai_prompt,
        "",
        "【拍品参数】",
        f"拍品名字：{name}",
        f"窑口：{kiln}",
        f"款识：{item.get('inscription', item.get('kiln', ''))}",
        f"年份：{item.get('era', '')}",
        f"尺寸：{item.get('size', '')}",
        f"容量：{item.get('capacity', '')}",
        f"品相：{item.get('condition', '')}",
        f"市场参考价：{item.get('estimate', '')}",
        f"起拍价：{item.get('starting_price', '')}元",
    ]

    if obsidian_context.strip():
        user_parts.append("")
        user_parts.append("【Obsidian 知识库参考】")
        user_parts.append(obsidian_context[:3000])

    if market_records:
        user_parts.append("")
        user_parts.append("【成交行情参考】")
        if market_analysis:
            user_parts.append(market_analysis)
        for r in market_records[:5]:
            user_parts.append(f"  - {r['name'][:40]} | 成交价: {r['deal_price']}元 | {r['auction_time']} | {r['source']}")
    return user_parts


async def generate_commentary_ai(item: dict) -> str:
    """
    使用 DeepSeek API 生成 AI 解说稿（方案2）

    流程:
    1. 解析 vault index.md → 定位窑口/器型/画片/工艺页面
    2. 读取完整 wiki 页面内容
    3. 查询成交行情 CSV
    4. 组装上下文 prompt 发送给 DeepSeek
    5. 返回 AI 生成的解说稿
    """
    name = item.get("name", "")
    kiln = item.get("kiln", "")

    # === 收集上下文 ===
    # Obsidian 知识（通过 vault index 导航 + 完整页面读取）
    obsidian_context = _gather_obsidian_context(item)

    # 成交行情
    xiaochashu_records = await search_auction_records(name, kiln) if name and kiln else []

    # === 组装 Prompt ===
    user_parts = [
        "请为以下拍品生成直播间解说稿：",
        "",
        "【拍品信息】",
        f"拍品名称：{name}",
        f"窑口：{item.get('kiln', '')}",
        f"年代：{item.get('era', '')}",
        f"材质：{item.get('material', '')}",
        f"尺寸：{item.get('size', '')}",
        f"估价：{item.get('estimate', '')}",
        f"起拍价：{item.get('starting_price', '')}元",
        f"品相：{item.get('condition', '')}",
    ]
    if item.get("description"):
        user_parts.append(f"描述：{item['description'][:150]}")

    if obsidian_context.strip():
        user_parts.append("")
        user_parts.append("【Obsidian 知识库全文】")
        user_parts.append(obsidian_context[:3000])  # 截断避免超 token

    if xiaochashu_records:
        user_parts.append("")
        user_parts.append("【成交行情参考】")
        analysis = generate_market_analysis(xiaochashu_records, kiln, name)
        if analysis:
            user_parts.append(analysis)
        user_parts.append("近期成交记录：")
        for r in xiaochashu_records[:5]:
            user_parts.append(
                f"  - {r['name'][:40]} | 成交价: {r['deal_price']}元 | "
                f"{r['auction_time']} | {r['source']}"
            )

    user_prompt = "\n".join(user_parts)
    logger.info(f"上下文长度: {len(user_prompt)} 字")

    # === 调用 DeepSeek API ===
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.8,
                "max_tokens": 1200,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        commentary = data["choices"][0]["message"]["content"].strip()
        logger.info(f"AI 解说稿生成成功: {len(commentary)} 字")
        return commentary
