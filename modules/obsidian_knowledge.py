"""
Obsidian 知识库搜索模块
搜索规则: 窑口必须匹配 + 拍品器型/纹饰/工艺/釉色定向搜索
每个搜索结果标记匹配类型(match_type)，用于解说稿定向提炼

数据源: Obsidian vault wiki/ 目录下的笔记文件
        - wiki/窑口/  — 窑口实体页
        - wiki/器型/  — 器型概念页
        - wiki/工艺/  — 工艺技法页
        - wiki/题材/  — 纹饰题材页
        - wiki/釉色/  — 釉色装饰页
"""
import csv
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import config
from utils.text_parser import extract_vessel, extract_motif, extract_craft

logger = logging.getLogger(__name__)

VAULT_PATH = Path(config.OBSIDIAN_VAULT_PATH)
VAULT_NAME = config.OBSIDIAN_VAULT_NAME

# 搜索子目录映射
WIKI_DIRS = {
    "kiln": VAULT_PATH / "wiki" / "窑口",
    "vessel": VAULT_PATH / "wiki" / "器型",
    "craft": VAULT_PATH / "wiki" / "工艺",
    "motif": VAULT_PATH / "wiki" / "题材",
    "glaze": VAULT_PATH / "wiki" / "釉色",
}

# 文件名匹配忽略（非实体页）
_IGNORE_FILES = {"_index.md", "index.md"}


def _frontmatter(file_path: Path) -> dict:
    """解析 markdown 文件的 YAML frontmatter，返回 dict"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm_text = text[3:end].strip()
    result = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            result[key] = val
    return result


def _make_obsidian_url(file_path: Path, heading: str = "") -> str:
    """生成 obsidian://open 协议链接"""
    rel = file_path.relative_to(VAULT_PATH).as_posix()
    # 去掉 .md 扩展名
    note_name = rel.replace(".md", "")
    url = f"obsidian://open?vault={quote(VAULT_NAME)}&file={quote(note_name)}"
    if heading:
        url += f"#{quote(heading)}"
    return url


def _make_file_url(file_path: Path) -> str:
    """生成 file:// 本地文件链接（obsidian:// 不可用时降级）"""
    return file_path.as_uri()


def _extract_content_sections(file_path: Path) -> list[dict]:
    """提取笔记的核心内容段落，按 ## 标题分块"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    # 跳过 frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        text = text[end + 3:] if end != -1 else text

    sections = []
    # 按 ## 标题分割（跳过 # 一级标题）
    blocks = re.split(r'\n(?=## )', text)
    for block in blocks:
        # 提取标题
        title_match = re.match(r'## (.+)', block)
        heading = title_match.group(1).strip() if title_match else "概览"
        # 清理正文
        body = block[title_match.end():] if title_match else block
        # 移除 ### 子标题行（如 "### 底款演进"）
        body = re.sub(r'^### .+$', '', body, flags=re.MULTILINE)
        # 移除 wiki 链接语法 [[xxx]] → xxx
        body = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', body)
        # 移除 markdown 格式
        body = re.sub(r'[*_>#|`]', '', body)
        body = re.sub(r'\n{3,}', '\n\n', body).strip()
        if len(body) > 20:
            sections.append({"heading": heading, "content": body})
    return sections


def _fuzzy_match_kiln(kiln_name: str) -> Optional[Path]:
    """在 wiki/窑口/ 中匹配窑口文件"""
    kiln_dir = WIKI_DIRS["kiln"]
    if not kiln_dir.exists():
        return None

    clean = kiln_name.strip()
    # 精确匹配文件名
    exact = kiln_dir / f"{clean}.md"
    if exact.exists():
        return exact

    # 遍历所有 md 文件，匹配文件名和 frontmatter 别名
    for f in sorted(kiln_dir.glob("*.md")):
        if f.name in _IGNORE_FILES:
            continue
        stem = f.stem
        if clean in stem or stem in clean:
            return f
        fm = _frontmatter(f)
        aliases = fm.get("aliases", "")
        if aliases:
            # aliases 可能是 "[xxx, yyy]" 格式
            alias_list = re.findall(r'[\w一-鿿]+', aliases)
            if any(clean in a or a in clean for a in alias_list):
                return f
    return None


def _search_by_keyword(keyword: str, match_type: str, max_results: int = 5) -> list[dict]:
    """
    按关键词在 wiki 子目录中搜索相关内容

    Args:
        keyword: 搜索词（如"春风祥玉 马蹄杯"）
        match_type: 匹配类型标记
            - "kiln_vessel": 窑口+器型
            - "kiln_motif": 窑口+纹饰/题材
            - "kiln_craft": 窑口+工艺
            - "kiln": 仅窑口
            - "general": 通用
    """
    results = []

    # 从 keyword 中分离窑口和其他词
    parts = keyword.split()
    kiln_part = parts[0] if parts else ""
    other_parts = parts[1:] if len(parts) > 1 else []

    # === 1. 搜索窑口文件 ===
    kiln_file = _fuzzy_match_kiln(kiln_part) if kiln_part else None
    kiln_sections = []
    if kiln_file:
        kiln_sections = _extract_content_sections(kiln_file)
        fm = _frontmatter(kiln_file)
        title = fm.get("title", kiln_file.stem)
        tags = fm.get("tags", "")

        # 窑口主页章节作为背景（始终标记为 kiln，不随 match_type 变化）
        for sec in kiln_sections[:3]:
            results.append({
                "title": f"{title} — {sec['heading']}",
                "snippet": sec["content"][:200],
                "content": sec["content"],
                "url": _make_obsidian_url(kiln_file, sec["heading"]),
                "file_url": _make_file_url(kiln_file),
                "source": f"Obsidian·窑口·{title}",
                "match_type": "kiln",  # 窑口文件内容统一标记为 kiln
            })

    # === 2. 按 match_type 搜索对应子目录 ===
    search_dirs = []
    if match_type == "kiln_vessel":
        search_dirs.append(WIKI_DIRS["vessel"])
    elif match_type == "kiln_motif":
        search_dirs.append(WIKI_DIRS["motif"])
    elif match_type == "kiln_craft":
        search_dirs.append(WIKI_DIRS["craft"])
        search_dirs.append(WIKI_DIRS["glaze"])
    elif match_type in ("kiln", "general"):
        search_dirs.extend([WIKI_DIRS["vessel"], WIKI_DIRS["motif"],
                           WIKI_DIRS["craft"], WIKI_DIRS["glaze"]])

    for search_dir in search_dirs:
        if not search_dir or not search_dir.exists():
            continue

        dir_name = search_dir.name  # 器型 / 工艺 / 题材 / 釉色

        for md_file in sorted(search_dir.glob("*.md")):
            if md_file.name in _IGNORE_FILES:
                continue

            fm = _frontmatter(md_file)
            title = fm.get("title", md_file.stem)
            tags = fm.get("tags", "")

            # 检查文件名/标题是否匹配搜索关键词的补充部分
            file_text = (md_file.stem + " " + title + " " + tags).lower()
            matched = False

            if not other_parts:
                # 仅窑口搜索：找与窑口相关的工艺/题材页
                # 检查窑口名是否出现在文件内容前 500 字符中
                try:
                    content_head = md_file.read_text(encoding="utf-8")[:1000]
                    if kiln_part and kiln_part in content_head:
                        matched = True
                except Exception:
                    pass
            else:
                # 检查补充词是否匹配文件名或标题
                for p in other_parts:
                    if p.lower() in file_text:
                        matched = True
                        break
                # 也检查文件正文（前 2000 字符）
                if not matched:
                    try:
                        body = md_file.read_text(encoding="utf-8")[:2000]
                        for p in other_parts:
                            if p in body:
                                matched = True
                                break
                    except Exception:
                        pass

            if not matched:
                continue

            sections = _extract_content_sections(md_file)
            for sec in sections[:2]:
                # 截取摘要
                snippet = sec["content"][:250]
                results.append({
                    "title": f"{title} — {sec['heading']}",
                    "snippet": snippet,
                    "content": sec["content"],
                    "url": _make_obsidian_url(md_file, sec["heading"]),
                    "file_url": _make_file_url(md_file),
                    "source": f"Obsidian·{dir_name}·{title}",
                    "match_type": match_type,
                })

    return results[:max_results]


async def search_item_intros_by_type(item_name: str, kiln_name: str) -> list[dict]:
    """
    按匹配类型搜索拍品介绍（从 Obsidian vault）
    - 窑口+器型 → wiki/窑口/ + wiki/器型/
    - 窑口+纹饰/题材 → wiki/窑口/ + wiki/题材/
    - 窑口+工艺 → wiki/窑口/ + wiki/工艺/ + wiki/釉色/
    每篇文章标记 match_type，用于解说稿定向提炼

    新增: 对窑口特点进行结构化分析，输出 analysis 字段

    Args:
        item_name: 拍品名称
        kiln_name: 窑口名称

    Returns:
        list[dict]: 包含 analysis 字段的搜索结果
    """
    if not item_name or not kiln_name:
        return []

    vessel = extract_vessel(item_name)
    motif = extract_motif(item_name)
    craft = extract_craft(item_name)

    all_results = []
    seen_titles = set()

    def _add_results(new_results):
        for r in new_results:
            key = r["title"]
            if key not in seen_titles:
                seen_titles.add(key)
                all_results.append(r)

    # === 窑口特点分析（新增） ===
    kiln_analysis = _analyze_kiln_characteristics(kiln_name)
    if kiln_analysis:
        # 将窑口分析作为第一条结果插入
        kiln_file = _fuzzy_match_kiln(kiln_name)
        analysis_item = {
            "title": f"{kiln_name} — 窑口特点分析",
            "snippet": kiln_analysis,
            "content": kiln_analysis,
            "url": _make_obsidian_url(kiln_file) if kiln_file else "",
            "file_url": _make_file_url(kiln_file) if kiln_file else "",
            "source": f"Obsidian·窑口分析·{kiln_name}",
            "match_type": "kiln_analysis",
        }
        all_results.append(analysis_item)
        seen_titles.add(analysis_item["title"])

    # 1. 窑口 + 器型
    if vessel:
        kw = f"{kiln_name} {vessel}"
        articles = _search_by_keyword(kw, match_type="kiln_vessel", max_results=3)
        _add_results(articles)
        logger.info(f"  Obsidian 窑口+器型 '{vessel}': {len(articles)} 条")

    # 2. 窑口 + 纹饰/题材
    if motif:
        kw = f"{kiln_name} {motif}"
        articles = _search_by_keyword(kw, match_type="kiln_motif", max_results=3)
        _add_results(articles)
        logger.info(f"  Obsidian 窑口+纹饰 '{motif}': {len(articles)} 条")

    # 3. 窑口 + 工艺
    if craft and craft != motif:
        kw = f"{kiln_name} {craft}"
        articles = _search_by_keyword(kw, match_type="kiln_craft", max_results=2)
        _add_results(articles)
        logger.info(f"  Obsidian 窑口+工艺 '{craft}': {len(articles)} 条")

    # 4. 仅窑口（兜底）
    articles = _search_by_keyword(kiln_name, match_type="kiln", max_results=3)
    _add_results(articles)
    logger.info(f"  Obsidian 仅窑口 '{kiln_name}': {len(articles)} 条")

    logger.info(f"Obsidian 拍品介绍总计: {len(all_results)} 条")
    return all_results[:config.OBSIDIAN_SEARCH_MAX_RESULTS * 2]


def _analyze_kiln_characteristics(kiln_name: str) -> str:
    """
    从窑口笔记中提取并生成窑口特点分析

    提取策略:
    1. 读取窑口 markdown 文件
    2. 从「基本信息」表格提取: 创立时间/创始人/所在地/主打品类/核心特色/地位
    3. 从「核心特色」章节提取关键段落
    4. 从「窑口简史」章节提取历史背景
    5. 整合为 150 字以内的精炼分析
    """
    kiln_file = _fuzzy_match_kiln(kiln_name)
    if not kiln_file:
        return ""

    try:
        text = kiln_file.read_text(encoding="utf-8")
    except Exception:
        return ""

    fm = _frontmatter(kiln_file)
    title = fm.get("title", kiln_file.stem)

    # 提取核心信息
    info_table = {}  # 基本信息表
    core_section = ""  # 核心特色
    history_section = ""  # 窑口简史

    # 去掉 frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        text = text[end + 3:] if end != -1 else text

    # 按 ## 标题分块
    blocks = re.split(r'\n(?=## )', text)
    for block in blocks:
        heading_match = re.match(r'## (.+)', block)
        heading = heading_match.group(1).strip() if heading_match else ""
        body = block[heading_match.end():].strip() if heading_match else block

        if "基本信息" in heading:
            # 解析表格
            for line in body.split("\n"):
                match = re.match(r'\| ([^|]+) \| ([^|]+) \|', line)
                if match:
                    key = match.group(1).strip()
                    val = match.group(2).strip()
                    # 移除 wiki 链接
                    val = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', val)
                    info_table[key] = val

        elif "核心特色" in heading:
            # 提取核心特色段落（跳过表格行、子标题、引用行）
            lines = []
            for line in body.split("\n"):
                line = line.strip()
                if not line or line.startswith("|") or line.startswith("-"):
                    continue
                if re.match(r'^#{1,4}\s', line):  # ### 底款演进 等子标题
                    continue
                if line.startswith(">"):  # blockquote
                    continue
                lines.append(line)
            core_section = "\n".join(lines)[:300]
            core_section = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', core_section)
            core_section = re.sub(r'[*_>#`]', '', core_section).strip()

        elif "窑口简史" in heading:
            # 提取窑口简史段落（跳过表格行、子标题）
            lines = []
            for line in body.split("\n"):
                line = line.strip()
                if not line or line.startswith("|") or line.startswith("-"):
                    continue
                if re.match(r'^#{1,4}\s', line):
                    continue
                if line.startswith(">"):
                    continue
                lines.append(line)
            history_section = "\n".join(lines)[:200]
            history_section = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', history_section)
            history_section = re.sub(r'[*_>#`]', '', history_section).strip()

    # 整合分析
    parts = []

    # 从基本信息表提取一句话概括
    era = info_table.get("创立时间", "")
    founder = info_table.get("创始人", "")
    location = info_table.get("所在地", "")
    specialty = info_table.get("主打品类", "") or info_table.get("核心特色", "")
    position = info_table.get("地位", "")

    if title:
        summary = f"{title}"
        if era:
            summary += f"，{era}"
        if founder:
            summary += f"由{founder}创立"
        if location:
            summary += f"，位于{location}"
        summary += "。"
        parts.append(summary)

    if specialty:
        parts.append(f"主打{specialty}。")

    # 从核心特色提取关键句（跳过冒号结尾、截断句）
    if core_section:
        raw_sentences = re.split(r'(?<=[。！？])', core_section)
        key_sentences = []
        for s in raw_sentences:
            s = s.strip()
            # 跳过太短、冒号结尾、末尾截断的句子
            if len(s) < 12:
                continue
            if s.endswith(('：', ':', '，', ',')):
                continue
            # 跳过以"详见"结尾的引用句
            if re.search(r'详见[\[（(]', s):
                continue
            key_sentences.append(s)
            if len(key_sentences) >= 2:
                break
        if key_sentences:
            parts.append("".join(key_sentences))

    # 从窑口简史提取一句话
    if history_section and not era:
        sentences = re.split(r'[。！\n]', history_section)
        key = [s.strip() for s in sentences if len(s.strip()) > 15][:1]
        if key:
            parts.append(key[0] + "。")

    if position:
        parts.append(f"{title}是{position}。")

    analysis = "".join(parts)
    # 限制 150 字
    if len(analysis) > 150:
        analysis = analysis[:145] + "。"

    return analysis


def get_kiln_file_path(kiln_name: str) -> Optional[str]:
    """获取窑口笔记文件的绝对路径（供外部使用）"""
    f = _fuzzy_match_kiln(kiln_name)
    return str(f) if f else None
