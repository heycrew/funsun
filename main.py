"""
拍品信息聚合系统 v3.0 - FastAPI 主入口
- 飞书 API 直读多维表格
- SSE 流式逐条处理拍品
- 直播间解说稿生成
"""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Query, Form, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from utils.browser import browser_manager
from modules.feishu_reader import read_feishu_page, is_feishu_url, classify_feishu_url
from modules.feishu_api import read_feishu_bitable
from modules.obsidian_knowledge import search_item_intros_by_type
from modules.obsidian_market import search_auction_records, generate_market_analysis
from modules.commentary_ai import generate_commentary_ai_direct, _gather_obsidian_context
from modules.funsun_cover import fetch_product_images
from modules.snapshot_cache import (compute_fingerprint, load_snapshot, save_snapshot,
                                     save_audio, get_audio, has_audio, acquire_lock, release_lock,
                                     update_item_in_snapshot)
from modules.auth import (authenticate, create_token, decode_token, get_current_user,
                           list_users, create_user, update_user, delete_user, change_password,
                           migrate_passwords)
from urllib.parse import quote as url_quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate_passwords()
    logger.info("启动浏览器管理器...")
    await browser_manager.start()
    logger.info("浏览器管理器已就绪")
    yield
    logger.info("关闭浏览器管理器...")
    await browser_manager.stop()


app = FastAPI(title="拍品信息聚合系统 v3.0", version="3.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ========== 登录路由 ==========

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/api/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "用户名或密码错误"})

    token = create_token(user["username"], user["role"])
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("funsun_token", token, httponly=True, max_age=86400)
    return resp


@app.get("/api/logout")
async def logout():
    resp = RedirectResponse(url="/login")
    resp.delete_cookie("funsun_token")
    return resp


# ========== 鉴权中间件 ==========

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    token = request.cookies.get("funsun_token")
    if not token or not decode_token(token):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/user-info")
async def user_info(request: Request):
    payload = get_current_user(request)
    return {"username": payload["username"], "role": payload["role"]}


# ========== 管理后台 ==========

CONFIG_FILE = Path(__file__).parent / "data" / "config.json"

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}

def _save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    payload = get_current_user(request)
    if payload.get("role") != "admin":
        return RedirectResponse(url="/")
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


# 用户管理 API
@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    get_current_user(request)
    return list_users()


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    get_current_user(request)
    body = await request.json()
    return create_user(body.get("username", ""), body.get("password", ""), body.get("role", "user"))


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    get_current_user(request)
    if not delete_user(user_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


# 配置管理 API
@app.get("/api/admin/config")
async def admin_get_config(request: Request):
    get_current_user(request)
    cfg = _load_config()
    cfg.setdefault("site_title", "FUNSUN拍卖信息聚合系统")
    cfg.setdefault("deepseek_api_key", os.getenv("DEEPSEEK_API_KEY", ""))
    cfg.setdefault("deepseek_base_url", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    cfg.setdefault("deepseek_model", os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))
    cfg.setdefault("commentary_template", "第[序号]号拍品,来自[拍品名字],窑口[窑口],款识[款识],年份[年份],尺寸[尺寸],容量[容量],品相[品相],市场参考价[参考价]")
    cfg.setdefault("ai_query_prompt", "请根据以上拍品信息,在知识库中查找对应的窑口背景、器型特点、画片题材和工艺技法,生成一段专业的拍卖解说词。")
    return cfg


@app.post("/api/admin/config")
async def admin_save_config(request: Request):
    get_current_user(request)
    cfg = await request.json()
    _save_config(cfg)
    return {"ok": True}


@app.post("/api/admin/change-password")
async def admin_change_password(request: Request):
    payload = get_current_user(request)
    body = await request.json()
    ok = change_password(payload["username"], body.get("old_password", ""), body.get("new_password", ""))
    if not ok:
        return JSONResponse({"error": "旧密码错误"}, status_code=400)
    return {"ok": True}


# ========== 解说稿生成 ==========

def generate_commentary(item: dict, item_intros: list[dict],
                        xiaochashu_records: list[dict] = None) -> str:
    """
    直播间解说稿 v2.0 — 融合 Obsidian 知识库 + 飞书拍品参数

    生成流程:
    1. 从飞书参数提取: 名称/窑口/年代/尺寸/估价/起拍价/材质/品相
    2. 从 Obsidian item_intros 提取:
       - kiln_analysis: 窑口背景（创始人/历史/地位）
       - kiln_vessel: 器型描述
       - kiln_motif: 画片/题材描述
       - kiln_craft: 工艺技法描述
    3. 融合生成 300 字专业解说稿
    """
    name = item.get("name", "这件拍品")
    kiln = item.get("kiln", "")
    era = item.get("era", "")
    size = item.get("size", "")
    material = item.get("material", "")
    estimate = item.get("estimate", "")
    starting_price = item.get("starting_price", "")
    condition = item.get("condition", "")
    description = item.get("description", "")

    # === 从 Obsidian 搜索结果中分类提取知识 ===
    kiln_bg = []      # 窑口背景
    vessel_info = []  # 器型描述
    motif_info = []   # 画片/题材描述
    craft_info = []   # 工艺描述
    general_info = [] # 通用补充

    # 标题碎片过滤器
    def _is_heading_fragment(text: str) -> bool:
        """检测是否为残留的标题/小标题碎片 或 无效句子"""
        if re.match(r'^[【\[\(（\|原\d]', text):
            return True
        # 冒号结尾 → 后面跟着列表/表格，不完整
        if text.endswith(('：', ':')):
            return True
        # 4-12字的短文本不含任何标点，大概率是标题残片
        if 4 <= len(text) <= 12 and not re.search(r'[。！，、；：，．]', text):
            return True
        return False

    for intro in (item_intros or []):
        content = intro.get("content", "") or intro.get("snippet", "")
        match_type = intro.get("match_type", "general")
        # 按句子边界切分（。！？：作为边界，保留标点）
        raw_sentences = re.split(r'(?<=[。！？：])', content)
        sentences = []
        for s in raw_sentences:
            s = s.strip()
            if len(s) > 8 and not _is_heading_fragment(s):
                sentences.append(s)

        for s in sentences[:3]:
            if match_type == "kiln_analysis":
                kiln_bg.append(s)
            elif match_type == "kiln_vessel":
                vessel_info.append(s)
            elif match_type == "kiln_motif":
                motif_info.append(s)
            elif match_type == "kiln_craft":
                craft_info.append(s)
            else:
                general_info.append(s)

    parts = []

    # ===== 第一段: 开场 + 拍品基本信息 =====
    parts.append(f"各位藏友，接下来给大家带来的这件——{name}。")

    # 窑口 + 年代 + 材质
    attr_parts = []
    if kiln:
        attr_parts.append(f"出自{kiln}")
    if era:
        attr_parts.append(f"{era}时期作品")
    if material:
        attr_parts.append(f"{material}材质")
    if size:
        attr_parts.append(f"尺寸{size}")
    if attr_parts:
        parts.append("，".join(attr_parts) + "。")

    # 品相
    if condition:
        parts.append(f"品相{condition}。")

    # ===== 第二段: 窑口背景（来自 Obsidian 窑口分析） =====
    if kiln_bg:
        # 取 2-3 句窑口核心信息
        bg_text = "".join(kiln_bg[:3])
        # 去掉开头重复的窑口名
        bg_text = re.sub(rf'^{kiln}[，,]\s*{kiln}[，,]', f'{kiln}，', bg_text)
        bg_text = re.sub(rf'^{kiln}[，,]\s*', '', bg_text)
        # 修复常见的数字/格式问题
        bg_text = re.sub(r'(?<!\d)0(?=\d{2,3}\s*年)', '20', bg_text)  # "007年" → "2007年"
        bg_text = re.sub(r'(?<!\d)0(?=\d\s*年)', '200', bg_text)     # "07年" → "2007年"
        # 清理表格残片和无意义空白
        bg_text = re.sub(r'\s{2,}', '，', bg_text)
        bg_text = re.sub(r'：[，,]\s*', '：', bg_text)
        # 截断过长的背景
        if len(bg_text) > 130:
            cut = bg_text.rfind('。', 0, 125)
            bg_text = bg_text[:cut + 1] if cut > 60 else bg_text[:125] + '。'
        parts.append(bg_text)
    elif general_info:
        parts.append(general_info[0] + "。")

    # 检查是否为无效摘要（纯数据/表格残留）
    def _is_garbage_text(text: str) -> bool:
        """检测是否为无法用于解说的无效文本"""
        # 包含过多顿号分隔 → 可能是枚举列表
        if text.count('、') > 4:
            return True
        # 非常短且无实际内容
        if len(text) < 8:
            return True
        return False

    # ===== 第三段: 器型 + 画片/题材 + 工艺 =====
    detail_parts = []

    if vessel_info and not _is_garbage_text(vessel_info[0]):
        v_text = vessel_info[0]
        if not v_text.endswith(('。', '！')):
            v_text += '。'
        detail_parts.append(f"器型上，{v_text}")

    if motif_info:
        m_text = motif_info[0]
        if not m_text.endswith(('。', '！')):
            m_text += '。'
        detail_parts.append(f"画片题材方面，{m_text}")

    if craft_info:
        c_text = craft_info[0]
        if not c_text.endswith(('。', '！')):
            c_text += '。'
        detail_parts.append(f"工艺技法上，{c_text}")

    # 如果 Obsidian 没有器型/画片/工艺信息，用飞书描述兜底
    if not detail_parts and description:
        desc_short = description[:120]
        if not desc_short.endswith(('。', '！')):
            desc_short += '。'
        detail_parts.append(f"{desc_short}")

    if detail_parts:
        parts.append("".join(detail_parts))

    # 如果没有 Obsidian 详细描述，补充通用背景
    if not (vessel_info or motif_info or craft_info):
        g_texts = []
        for g in general_info[:2]:
            if not g.endswith(('。', '！')):
                g += '。'
            g_texts.append(g)
        if g_texts:
            parts.append("".join(g_texts))

    # ===== 第四段: 市场参考 =====
    market_parts = []
    if estimate:
        market_parts.append(f"本件拍品估价{estimate}")
    if starting_price:
        market_parts.append(f"起拍价{starting_price}元")

    # 成交行情参考
    if xiaochashu_records:
        prices = []
        for r in xiaochashu_records[:5]:
            try:
                prices.append(float(r.get("deal_price", "0").replace(",", "")))
            except:
                pass
        if prices:
            avg_m = int(sum(prices) // len(prices))
            max_m = int(max(prices))
            min_m = int(min(prices))
            market_parts.append(f"同窑口同类拍品近期成交价{min_m}到{max_m}元，均价约{avg_m}元")

    if market_parts:
        parts.append("，".join(market_parts) + "。")

    # ===== 结尾: 竞价引导 =====
    parts.append("喜欢的朋友不要错过，现在开始竞价！")

    result = "".join(parts)
    # 清理多余空白
    result = re.sub(r'\s{2,}', '', result)
    result = re.sub(r'\n+', '', result)
    # 控制在 400 字以内
    if len(result) > 400:
        # 在句子边界截断
        cut = result.rfind("。", 0, 380)
        if cut > 200:
            result = result[:cut + 1] + "喜欢的朋友不要错过，现在开始竞价！"
        else:
            result = result[:380] + "……喜欢的朋友不要错过！"

    return result


# ========== API 路由 ==========

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"request": request}
    )


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "拍品信息聚合系统 v3.0"}


@app.get("/api/cached-audio")
async def cached_audio(index: int = Query(...), audio_type: str = "commentary"):
    """获取缓存的音频文件"""
    from fastapi.responses import Response
    audio = get_audio(index, audio_type)
    if audio:
        return Response(content=audio, media_type="audio/mpeg")
    return Response(status_code=404)


@app.get("/api/item-cover")
async def item_cover(code: str = Query(...)):
    """根据拍品ID获取所有拍品图URL"""
    if not code:
        return {"code": code, "images": []}
    images = await fetch_product_images(code)
    return {"code": code, "images": images}


class RefreshItemRequest(BaseModel):
    name: str = ""
    kiln: str = ""
    inscription: str = ""
    code: str = ""
    index: int = -1
    starting_price: str = ""
    estimate: str = ""
    era: str = ""
    size: str = ""
    capacity: str = ""
    material: str = ""
    condition: str = ""
    description: str = ""


class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    index: int = -1
    audio_type: str = "commentary"  # commentary 或 template


@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    """文字转语音 — 火山引擎 v3"""
    import uuid, httpx
    from fastapi.responses import Response

    if not req.text or len(req.text) > 1000:
        return Response(status_code=400)

    voice = req.voice or config.VOLCANO_TTS_VOICE
    resource_id = "seed-icl-2.0" if voice.startswith("S_") else config.VOLCANO_TTS_RESOURCE_ID
    cfg = _load_config()
    tts_ctx = cfg.get("tts_context") or config.VOLCANO_TTS_CONTEXT
    logger.info(f"TTS请求: voice={voice} resource={resource_id} text_len={len(req.text)}")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
                headers={
                    "X-Api-Key": config.VOLCANO_TTS_TOKEN,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                    "Content-Type": "application/json",
                },
                json={
                    "user": {"uid": "auction_user"},
                    "req_params": {
                        "text": req.text,
                        "speaker": voice,
                        "audio_params": {"format": "mp3", "sample_rate": 24000},
                        "additions": json.dumps({"context_texts": tts_ctx}) if tts_ctx else None,
                    },
                },
            )
            if resp.status_code == 200:
                import base64
                # V3 返回多行 JSON: {"code":0,"data":"..."}\n{"code":0,"data":"..."}\n...
                audio_parts = []
                for line in resp.text.strip().split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    code = chunk.get("code", -1)
                    if code == 3000:
                        return Response(status_code=503, content="TTS quota exceeded")
                    if code not in (0, 20000000):  # 0=数据块, 20000000=结束标记
                        logger.warning(f"火山引擎 TTS chunk code={code}: {chunk.get('message','')}")
                        continue
                    b64 = chunk.get("data", "")
                    if b64:
                        audio_parts.append(base64.b64decode(b64))
                if audio_parts:
                    audio_bytes = b"".join(audio_parts)
                    if req.index >= 0:
                        save_audio(req.index, audio_bytes, req.audio_type)
                    return Response(content=audio_bytes, media_type="audio/mpeg")
            logger.warning(f"火山引擎 TTS 失败: {resp.status_code}")
    except Exception as e:
        logger.error(f"TTS 异常: {e}")
    return Response(status_code=503)


@app.post("/api/refresh/commentary")
async def refresh_commentary(req: RefreshItemRequest):
    """单独刷新某件拍品的解说稿"""
    item = {
        "name": req.name,
        "kiln": req.kiln,
        "inscription": req.inscription,
        "code": req.code,
        "starting_price": req.starting_price,
        "estimate": req.estimate,
        "era": req.era,
        "size": req.size,
        "capacity": req.capacity,
        "material": req.material,
        "condition": req.condition,
        "description": req.description,
    }
    # 用 direct 版本：复用 SSE 流程的两阶段逻辑
    from modules.commentary_ai import _gather_obsidian_context, generate_commentary_ai_direct
    from modules.obsidian_market import search_auction_records, generate_market_analysis
    obs_ctx = _gather_obsidian_context(item) if req.name and req.kiln else ""
    records = await search_auction_records(req.name, req.kiln) if req.name and req.kiln else []
    mkt = generate_market_analysis(records, req.kiln, req.name) if records else ""
    commentary = await generate_commentary_ai_direct(item, obs_ctx, records, mkt, req.index)
    # 同步生成固定介绍文案
    from modules.commentary_ai import _load_commentary_config, _render_template
    cfg = _load_commentary_config()
    tpl_text = _render_template(cfg.get("commentary_template", ""), item, req.index)
    # 更新缓存快照
    if req.index >= 0:
        update_item_in_snapshot(req.index, commentary=commentary)
    return {"commentary": commentary, "template_text": tpl_text}


@app.post("/api/cache/update-commentary")
async def cache_update_commentary(request: Request):
    """编辑保存后更新缓存"""
    body = await request.json()
    idx = body.get("index", -1)
    commentary = body.get("commentary", "")
    if idx >= 0 and commentary:
        update_item_in_snapshot(idx, commentary=commentary)
        return {"ok": True}
    return {"ok": False}


@app.post("/api/refresh/market")
async def refresh_market(req: RefreshItemRequest):
    """单独刷新某件拍品的成交行情"""
    xiaochashu_records = await search_auction_records(req.name, req.kiln)
    market_analysis = generate_market_analysis(xiaochashu_records, req.kiln, req.name) if xiaochashu_records else ""
    return {
        "market_analysis": market_analysis,
        "xiaochashu_records": xiaochashu_records[:10],
    }


@app.get("/api/analyze-all")
async def analyze_all(feishu_url: str = Query(...)):
    """
    SSE 端点：读取飞书多维表格全部拍品，逐条搜索参考资料，实时推送结果

    事件格式:
      data: {"type":"start",  "total":35, "items":[{摘要}]}
      data: {"type":"progress","index":0, "status":"searching"}
      data: {"type":"result",  "index":0, "data":{拍品+参考资料+解说稿}}
      data: {"type":"done"}
    """
    async def event_stream() -> AsyncGenerator[str, None]:
        locked = False
        try:
            if not is_feishu_url(feishu_url):
                yield f"data: {json.dumps({'type': 'error', 'message': '无效的飞书链接'}, ensure_ascii=False)}\n\n"
                return

            # 并发锁检查
            if not acquire_lock():
                yield f"data: {json.dumps({'type': 'error', 'message': '当前有任务分析中，请稍后重试'}, ensure_ascii=False)}\n\n"
                return
            locked = True

            page_type = classify_feishu_url(feishu_url)
            items = []

            # 读取拍品列表
            if page_type in ("bitable", "base"):
                items = await read_feishu_bitable(feishu_url)
            else:
                # 文档模式只处理一个
                single = await read_feishu_page(feishu_url)
                items = [single]

            if not items:
                yield f"data: {json.dumps({'type': 'error', 'message': '未找到拍品数据'}, ensure_ascii=False)}\n\n"
                return

            # 增量缓存：按 code 逐条对比
            from modules.snapshot_cache import item_fingerprint, build_code_index, delete_audio_for_code
            snapshot = load_snapshot()
            snap_index = build_code_index(snapshot) if snapshot else {}

            # 给每条 item 分配新 index，标记复用或重分析
            cached_results = []   # 直接复用的缓存结果
            items_to_process = [] # 需要重新分析的 items
            items_for_snapshot = [None] * len(items)  # 最终快照（按新 index 排列）

            for new_idx, item in enumerate(items):
                code = item.get("code", "")
                fp = item_fingerprint(item)
                old = snap_index.get(code)
                if old and old.get("_fp") == fp:
                    # 同 code 同指纹 → 复用，只更新 index
                    old_item = dict(old)
                    old_item["index"] = new_idx
                    old_item["_cached"] = True
                    old_item["_changed"] = False
                    idx_val = new_idx
                    old_item["_has_audio"] = has_audio(idx_val) or has_audio(idx_val, "template")
                    cached_results.append(old_item)
                    items_for_snapshot[new_idx] = old_item
                else:
                    # 新增或变更 → 需要重新分析
                    if old:
                        delete_audio_for_code(code)  # 清理旧音频
                    item["_new_idx"] = new_idx
                    items_to_process.append((new_idx, item))
                    # 占位，等分析完填充
                    items_for_snapshot[new_idx] = {"index": new_idx, "code": code, "name": item.get("name",""), "kiln": item.get("kiln","")}

            logger.info(f"增量分析: 复用{len(cached_results)}条, 新分析{len(items_to_process)}条")

            # 发送初始数据
            summaries = [{
                "index": i, "name": it.get("name", ""), "kiln": it.get("kiln", ""),
                "inscription": it.get("inscription", ""), "code": it.get("code", ""),
                "starting_price": it.get("starting_price", ""), "estimate": it.get("estimate", ""),
            } for i, it in enumerate(items)]
            yield f"data: {json.dumps({'type': 'start', 'total': len(items), 'items': summaries}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.1)

            # 先推送复用的缓存结果
            for cr in cached_results:
                cr["_cached"] = True; cr["_changed"] = False
                yield f"data: {json.dumps({'type': 'result', 'data': cr}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.03)

            # 如果全部复用，直接结束
            if not items_to_process:
                # 保存快照（更新 index 顺序）
                save_snapshot([it for it in items_for_snapshot if it is not None], compute_fingerprint(items))
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

            # ====== 只处理需要重分析的 items ======
            CHUNK_SIZE = 3
            from pathlib import Path as _Path
            from modules.obsidian_knowledge import get_kiln_file_path

            pre_data = []
            for new_idx, item in items_to_process:
                name = item.get("name", "")
                kiln_name = item.get("kiln", "")
                yield f"data: {json.dumps({'type': 'progress', 'index': new_idx, 'status': 'searching'}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.05)

                obs_ctx = _gather_obsidian_context(item) if name and kiln_name else ""
                records = await search_auction_records(name, kiln_name) if name and kiln_name else []
                mkt = generate_market_analysis(records, kiln_name, name) if records else ""
                obs_url = ""
                if kiln_name:
                    kp = get_kiln_file_path(kiln_name)
                    if kp:
                        obs_url = f"obsidian://open?vault={url_quote('当代茶艺瓷器')}&file={url_quote(_Path(kp).stem)}"
                pre_data.append({"idx": new_idx, "item": item, "name": name, "kiln": kiln_name,
                    "obs_ctx": obs_ctx, "records": records, "market": mkt, "obs_url": obs_url,
                    "_fp": item_fingerprint(item)})

            # 阶段2：纯 HTTP 并发（所有操作都是 async，无文件 IO）
            import anyio
            all_results = []  # 收集结果用于缓存
            for chunk_start in range(0, len(pre_data), CHUNK_SIZE):
                chunk = pre_data[chunk_start:chunk_start + CHUNK_SIZE]

                async def call_ai(pd):
                    c = await generate_commentary_ai_direct(pd["item"], pd["obs_ctx"], pd["records"], pd["market"], pd["idx"])
                    # 固定模板文本（用于单独生成语音）
                    from modules.commentary_ai import _load_commentary_config, _render_template
                    cfg2 = _load_commentary_config()
                    tpl = cfg2.get("commentary_template", "")
                    tpl_text = _render_template(tpl, pd["item"], pd["idx"])
                    return {
                        "index": pd["idx"], "name": pd["name"], "kiln": pd["kiln"],
                        "template_text": tpl_text,
                        "inscription": pd["item"].get("inscription", ""),
                        "starting_price": pd["item"].get("starting_price", ""),
                        "estimate": pd["item"].get("estimate", ""),
                        "era": pd["item"].get("era", ""), "size": pd["item"].get("size", ""),
                        "capacity": pd["item"].get("capacity", ""),
                        "material": pd["item"].get("material", ""), "code": pd["item"].get("code", ""),
                        "condition": pd["item"].get("condition", ""),
                        "description": pd["item"].get("description", ""),
                        "xiaochashu_records": pd["records"][:10],
                        "commentary": c, "market_analysis": pd["market"],
                        "obsidian_kiln_url": pd["obs_url"],
                    }

                results = []
                async def run(pd):
                    r = await call_ai(pd)
                    results.append(r)

                async with anyio.create_task_group() as tg:
                    for pd in chunk:
                        tg.start_soon(run, pd)

                for r in results:
                    r["_cached"] = False
                    r["_changed"] = True
                    all_results.append(r)
                    # 更新 items_for_snapshot
                    idx = r.get("index", -1)
                    if 0 <= idx < len(items_for_snapshot):
                        r["_fp"] = item_fingerprint(items[idx]) if idx < len(items) else ""
                        items_for_snapshot[idx] = r
                    yield f"data: {json.dumps({'type': 'result', 'data': r}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.1)

            # 合并缓存 + 新分析 → 保存快照
            final_snapshot = [it for it in items_for_snapshot if it is not None]
            if final_snapshot:
                save_snapshot(final_snapshot, compute_fingerprint(items))
                logger.info(f"快照已保存: {len(final_snapshot)} 条")

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            logger.info("客户端断开连接，SSE 已停止")
        except Exception as e:
            logger.exception(f"SSE错误: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if locked:
                release_lock()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
