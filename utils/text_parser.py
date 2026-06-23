"""
文本解析工具
用于从飞书页面文本中提取拍品结构化信息
支持飞书文档常见的字段命名格式
"""
import re
from typing import Optional


def extract_auction_info(text: str) -> dict:
    """
    从页面文本中提取拍品结构化信息
    覆盖飞书文档中常见的字段命名变体

    飞书文档常见字段名：
    - 拍品名字 / 拍品名称 / 品名 / 名称 / 商品名称
    - 窑口 / 窑口名称 / 窑场 / 所属窑口
    - 编号 / 拍品编号 / Lot / 拍品号
    - 尺寸 / 规格 / 大小 / 高×宽
    - 起拍价 / 起拍 / 起拍价格 / 起价
    - 估价 / 参考价 / 预估价
    - 年代 / 朝代 / 时期 / 年份
    - 材质 / 质地 / 胎质 / 釉质
    - 描述 / 拍品描述 / 详情 / 说明
    """
    info = {
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
        "full_text": text,
    }

    # ========== 拍品名称 ==========
    # 飞书文档常用: 拍品名字、拍品名称、品名、名称
    name_patterns = [
        # 精确匹配: 拍品名字：xxx  或  拍品名字: xxx
        r"(?:拍品名字|拍品名|拍品名称|品名|商品名称|名称)[：:]\s*(.+?)(?:\n|$)",
        # 带括号/方括号标记
        r"【(?:拍品名字|拍品名|拍品名称|品名|名称)】\s*(.+?)(?:\n|$|【)",
        r"『(?:拍品名字|拍品名|拍品名称|品名|名称)』\s*(.+?)(?:\n|$|『)",
        # Lot格式: Lot 001：xxx拍品
        r"(?:拍品|Lot\.?)\s*(\d+)[：:]\s*(.+?)(?:\n|$)",
        # 带引号的名称
        r"「(.+?)」\s*(?:拍品|拍卖)",
        r"\"(.+?)\"\s*(?:拍品|拍卖)",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text)
        if match:
            # 处理多捕获组情况（如 Lot 001：xxx拍品）
            if len(match.groups()) > 1 and match.group(2):
                if not info["code"]:
                    info["code"] = match.group(1).strip()
                info["name"] = match.group(2).strip()
            else:
                info["name"] = match.group(1).strip()
            # 清理名称末尾的常见后缀
            info["name"] = re.sub(r'\s*(?:拍品|拍卖|一[件对套组])$', '', info["name"])
            break

    # ========== 编号 ==========
    code_patterns = [
        r"(?:编号|拍品编号|拍品号|Lot\.?|序号)[：:]\s*([A-Za-z0-9\-_\s]+?)(?:\n|$)",
        r"第\s*(\d+)\s*号",
        r"Lot\.?\s*(\d+)",
    ]
    if not info["code"]:
        for pattern in code_patterns:
            match = re.search(pattern, text)
            if match:
                info["code"] = match.group(1).strip()
                break

    # ========== 窑口名称 ==========
    # 飞书文档常用: 窑口、窑口名称、窑场、所属窑口
    kiln_patterns = [
        # 明确的字段标签
        r"(?:窑口名称|窑口|窑场|瓷窑|所属窑口|窑口类型)[：:]\s*(.+?)(?:\n|$|[，。,\.])",
        # 中文窑口名（2-4字 + 窑/窑口/窑系）
        r"([一-鿿]{2,4}(?:窑|窑口|窑系))",
        # 描述语境
        r"(?:出自|源于|来自|属于)\s*([一-鿿]{2,}(?:窑|窑口))",
        # 带括号
        r"【(?:窑口|窑口名称)】\s*(.+?)(?:\n|$|【)",
    ]
    for pattern in kiln_patterns:
        match = re.search(pattern, text)
        if match:
            kiln = match.group(1).strip()
            # 过滤太泛的匹配
            if kiln and len(kiln) >= 2 and kiln not in ("窑口", "窑场", "瓷窑", "窑系"):
                info["kiln"] = kiln
                break

    # ========== 尺寸 ==========
    size_patterns = [
        r"(?:尺寸|规格|大小|器型尺寸)[：:]\s*(.+?)(?:\n|$)",
        r"【(?:尺寸|规格|大小)】\s*(.+?)(?:\n|$|【)",
        r"(?:高|高度)\s*[：:]\s*([\d.]+\s*(?:cm|厘米|公分)?)\s*.*?(?:直径|口径|宽)\s*[：:]\s*([\d.]+\s*(?:cm|厘米|公分)?)",
        r"([\d.]+\s*[×xX×]\s*[\d.]+\s*[×xX×]?\s*[\d.]*)\s*(?:cm|厘米|公分)?",
    ]
    for pattern in size_patterns:
        match = re.search(pattern, text)
        if match:
            if len(match.groups()) > 1 and match.group(2):
                info["size"] = f"高{match.group(1)} 直径{match.group(2)}"
            else:
                info["size"] = match.group(1).strip()
            break

    # ========== 起拍价 ==========
    price_patterns = [
        r"(?:起拍价|起拍|起拍价格|起价|起售价)[：:]\s*(.+?)(?:\n|$)",
        r"【(?:起拍价|起拍|起拍价格|起价)】\s*(.+?)(?:\n|$|【)",
        r"(?:起拍价|起拍)\s*[：:]*\s*(?:RMB|CNY|￥)?\s*([\d,]+\.?\d*)\s*(?:元|万元|万)?",
        r"(?:起拍价|起拍)\s*[：:]*\s*(?:RMB|CNY|￥)?\s*([\d,]+\.?\d*[万万千]?)",
    ]
    for pattern in price_patterns:
        match = re.search(pattern, text)
        if match:
            info["starting_price"] = match.group(1).strip()
            break

    # ========== 估价 ==========
    estimate_patterns = [
        r"(?:估价|参考价|市场估价|预估价|估值|市场参考价)[：:]\s*(.+?)(?:\n|$)",
        r"【(?:估价|参考价|预估价|估值)】\s*(.+?)(?:\n|$|【)",
        r"(?:估价|估值)\s*[：:]*\s*(?:RMB|CNY|￥)?\s*([\d,\-～~]+)\s*(?:万|元|千)?",
    ]
    for pattern in estimate_patterns:
        match = re.search(pattern, text)
        if match:
            info["estimate"] = match.group(1).strip()
            break

    # ========== 年代 ==========
    era_patterns = [
        r"(?:年代|时代|朝代|时期|年份|制作年代)[：:]\s*(.+?)(?:\n|$)",
        r"【(?:年代|时代|朝代|时期)】\s*(.+?)(?:\n|$|【)",
        r"(清[朝代]?|明[朝代]?|宋[朝代]?|元[朝代]?|唐[朝代]?|民国|现代|当代|近现代)",
        r"((?:清|明|宋|元|唐)(?:朝|代)\s*(?:[初中末晚]期)?)",
    ]
    for pattern in era_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                era = match.group(1).strip()
            except IndexError:
                era = match.group(0).strip()
            if era and len(era) >= 2:
                info["era"] = era
                break

    # ========== 材质 ==========
    material_patterns = [
        r"(?:材质|质地|胎质|釉质|工艺类别)[：:]\s*(.+?)(?:\n|$)",
        r"【(?:材质|质地|胎质|工艺)】\s*(.+?)(?:\n|$|【)",
        r"(青花|粉彩|五彩|斗彩|釉里红|珐琅彩|白瓷|青瓷|黑瓷|钧瓷|定瓷|汝瓷|哥瓷|官瓷|龙泉瓷|德化瓷|景德镇瓷)",
    ]
    for pattern in material_patterns:
        match = re.search(pattern, text)
        if match:
            info["material"] = match.group(1).strip()
            break

    # ========== 描述 ==========
    desc_patterns = [
        r"(?:拍品描述|描述|详情|说明|拍品说明|详细介绍)[：:]\s*(.+?)(?:\n\n|\n(?:拍品|编号|窑口|尺寸|起拍|估价|年代|材质)|\Z)",
    ]
    for pattern in desc_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            desc = match.group(1).strip()
            if len(desc) > 10:
                info["description"] = desc[:500]  # 截取前500字
                break

    # ========== 如果没有直接描述，提取第一段较长文本作为描述 ==========
    if not info["description"]:
        # 找第一个超过30字的连续段落
        paragraphs = re.split(r'\n\s*\n', text)
        for para in paragraphs:
            para = para.strip()
            if len(para) > 30 and not any(
                kw in para[:20] for kw in ["拍品名称", "拍品名字", "编号", "尺寸", "起拍", "估价", "年代", "材质", "窑口"]
            ):
                info["description"] = para[:500]
                break

    return info


def extract_kiln_from_dom_text(text: str) -> Optional[str]:
    """
    从DOM文本中专门提取窑口信息
    适用于飞书文档中窑口字段紧随标签出现的情况
    """
    # 飞书文档中常见的窑口字段格式
    patterns = [
        r'窑口[：:\s]*([一-鿿]{2,6}(?:窑|窑口|窑系)?)',
        r'窑口名称[：:\s]*([一-鿿]{2,6}(?:窑|窑口|窑系)?)',
        r'所属窑口[：:\s]*([一-鿿]{2,6}(?:窑|窑口|窑系)?)',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip()
    return None


def extract_name_from_dom_text(text: str) -> Optional[str]:
    """
    从DOM文本中专门提取拍品名称
    适用于飞书文档中名称字段紧随标签出现的情况
    """
    patterns = [
        r'拍品名字[：:\s]*(.+?)(?:\n|$)',
        r'拍品名[：:\s]*(.+?)(?:\n|$)',
        r'拍品名称[：:\s]*(.+?)(?:\n|$)',
        r'品名[：:\s]*(.+?)(?:\n|$)',
        r'名称[：:\s]*(.+?)(?:\n|$)',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            name = m.group(1).strip()
            if name and len(name) >= 2:
                return name
    return None


def extract_keywords(info: dict) -> list[str]:
    """
    从拍品信息中提取搜索关键词列表
    返回按重要程度排序的关键词列表
    """
    keywords = []

    if info.get("name"):
        keywords.append(info["name"])

    if info.get("kiln") and info.get("name"):
        short = extract_short_name(info["name"])
        keywords.append(f"{info['kiln']} {short}")

    if info.get("kiln"):
        keywords.append(info["kiln"])

    parts = []
    if info.get("era"):
        parts.append(info["era"])
    if info.get("material"):
        parts.append(info["material"])
    if parts:
        keywords.append(" ".join(parts))

    return keywords


def extract_short_name(name: str) -> str:
    """从长拍品名称中提取简短关键词用于搜索"""
    if not name:
        return ""
    cleaned = re.sub(r'^(清[朝代]?|明[朝代]?|宋[朝代]?|元[朝代]?|唐[朝代]?|民国|现代|当代|近现代)', '', name)
    cleaned = re.sub(r'(一[件对套组]$|\(.*\)$|（.*）$|\s*拍品$|\s*拍卖$)', '', cleaned)
    return cleaned.strip() or name


# ========== 题材/纹饰 和 器型 提取 ==========

# 常见工艺/技法
CRAFT_PATTERNS = [
    "青花暗刻", "青花釉里红", "青花点彩", "釉里红", "暗刻",
    "青花", "粉彩", "五彩", "斗彩", "珐琅彩", "洒蓝", "蓝地",
    "外青花内青花釉里红", "仿汝窑", "粉青釉", "开片",
    "颜色釉", "白瓷", "青瓷", "黑瓷",
]


def extract_craft(item_name: str) -> str:
    """从拍品名中提取工艺/技法"""
    if not item_name:
        return ""
    best = ""
    for c in CRAFT_PATTERNS:
        if c in item_name and len(c) > len(best):
            best = c
    return best


# 常见陶瓷器型
VESSEL_PATTERNS = [
    "马蹄杯", "缸杯", "压手杯", "撇口杯", "炉式杯", "高足杯", "直口杯",
    "手雷杯", "玉兰杯", "鸡心杯", "斗笠杯", "铃铛杯", "仰钟杯", "罗汉杯",
    "公道杯", "公道", "盖碗", "三才盖碗", "壶承", "香炉", "水洗", "笔筒",
    "果盘", "将军罐", "天球瓶", "梅瓶", "玉壶春", "赏瓶", "抱月瓶",
    "杯", "碗", "壶", "盘", "罐", "洗", "炉", "瓶", "碟", "盅",
]

# 常见纹饰/题材
MOTIF_PATTERNS = [
    "事事如意纹", "缠枝莲纹", "缠枝花卉纹", "鱼藻纹", "梅花纹", "如意纹",
    "八仙纹", "山水纹", "冰梅纹", "暗刻纹", "莲塘纹", "狮子绣球纹",
    "岁寒三友纹", "折枝纹", "龙凤纹", "花卉纹", "缠枝纹", "灵芝纹",
    "杂宝纹", "海浪纹", "秋思芦雁图", "和合二仙", "五路财神",
    "一路连科", "满地梅花纹", "洞石芭蕉纹", "写意纹",
    "青花", "釉里红", "粉彩", "五彩", "斗彩", "珐琅彩",
]


def extract_vessel(item_name: str) -> str:
    """从拍品名中提取器型（宽泛匹配）

    示例: 马蹄杯→杯, 缸杯→杯, 压手杯→杯, 公道杯→杯, 盖碗→碗
    优先匹配多字器型（如"马蹄杯"优先于"杯"），无多字匹配时用通用器型
    """
    if not item_name:
        return ""

    # 1. 先找多字具体器型（带修饰的）
    best = ""
    for v in VESSEL_PATTERNS:
        if v in item_name and len(v) > len(best) and len(v) >= 3:
            best = v
    if best:
        return best

    # 2. 无多字器型时，找通用器型
    generic_vessels = ["杯", "碗", "壶", "盘", "罐", "洗", "炉", "瓶", "碟", "盅", "盒", "盆", "缸"]
    for g in generic_vessels:
        if g in item_name:
            return g

    return ""


def extract_motif(item_name: str) -> str:
    """从拍品名中提取主要纹饰/题材"""
    if not item_name:
        return ""
    best = ""
    for m in MOTIF_PATTERNS:
        if m in item_name and len(m) > len(best):
            best = m
    return best


def extract_search_keywords(item_name: str, kiln: str = "") -> list[str]:
    """
    从拍品名中提取搜索关键词组合
    窑口 + 题材 + 器型
    返回按优先级排列的关键词列表
    """
    vessel = extract_vessel(item_name)
    motif = extract_motif(item_name)

    keywords = []

    # 1. 窑口 + 器型（最佳匹配）
    if kiln and vessel:
        keywords.append(f"{kiln} {vessel}")

    # 2. 窑口 + 题材
    if kiln and motif:
        keywords.append(f"{kiln} {motif}")

    # 3. 仅窑口
    if kiln:
        keywords.append(kiln)

    # 4. 器型单独
    if vessel and vessel not in keywords:
        keywords.append(vessel)

    # 5. 题材单独
    if motif and motif not in keywords:
        keywords.append(motif)

    return keywords
