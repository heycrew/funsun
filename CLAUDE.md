# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

拍品信息聚合系统 — FastAPI Web 应用，从飞书多维表格读取拍品数据，结合 Obsidian 本地知识库生成直播间解说稿和成交行情分析。

## 环境依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

## 启动 / 停止

```bash
# 启动
cd action && python -m uvicorn main:app --host 0.0.0.0 --port 8000 &

# 停止
pkill -f "uvicorn main:app"
```

访问 `http://localhost:8000`，点击「开始分析」即可。飞书链接已固定（隐藏输入框），无需手动输入。

## 核心架构

### 数据流（并发架构）

```
飞书多维表格 API (feishu_api.py)
    │
    ├─ 指纹对比 (snapshot_cache.py)
    │   ├─ 命中 → 直接推送缓存（秒开）
    │   └─ 未命中 ↓
    │
    ├─ Phase 1: Obsidian wiki/ + CSV (串行, ~100ms/条)
    │
    └─ Phase 2: DeepSeek API (并发 3条/批, anyio task group)
         └──→ 解说稿 → SSE 推送 → 保存快照
```

### 关键模块

| 模块 | 职责 |
|------|------|
| `main.py` | FastAPI 入口、SSE 流式端点、解说稿生成、TTS API、Refresh API |
| `modules/commentary_ai.py` | AI 解说稿生成（DeepSeek API）+ `generate_commentary_ai_direct()` 纯 HTTP 版用于并发 |
| `modules/snapshot_cache.py` | 分析结果快照缓存：指纹对比、JSON 持久化、音频存取、文件锁 |
| `utils/browser.py` | Playwright 浏览器管理器（全局单例，含飞书页面读取） |
| `modules/obsidian_knowledge.py` | Obsidian wiki/ 搜索：窑口/器型/工艺/题材/釉色，窑口特点分析 |
| `modules/obsidian_market.py` | 成交行情 CSV 搜索（三级权重排序）+ 行情分析生成 |
| `modules/funsun_cover.py` | 小茶书管理后台封面图抓取（Playwright 登录 + 持久化 Cookie + 多图列表） |
| `modules/feishu_api.py` | 飞书 Open API 直读多维表格 |
| `modules/feishu_reader.py` | 飞书页面 DOM 读取（Playwright 浏览器，文档模式兜底） |
| `utils/text_parser.py` | 拍品名拆解（器型/纹饰/工艺提取） |
| `utils/browser.py` | Playwright 浏览器管理器（全局单例，含飞书页面读取） |

### 数据源路径（config.py）

- `OBSIDIAN_VAULT_PATH` = `D:\WorkDoc\当代茶艺瓷器`
- `OBSIDIAN_MARKET_CSV` = `raw/数据/成交行情.csv`（17K+ 条记录）
- 知识库：`wiki/窑口/`、`wiki/器型/`、`wiki/工艺/`、`wiki/题材/`、`wiki/釉色/`、`wiki/鉴赏/`

### 定价/排序

- `obsidian_market._score_match()`：窑口+100 → 器型+30 → 画片+20 → 工艺+10 → 价格+1~3
- `obsidian_knowledge._analyze_kiln_characteristics()`：从基本信息表+核心特色+窑口简史生成 ≤150 字分析
- `config.py` TTS 配置：`VOLCANO_TTS_TOKEN`、`VOLCANO_TTS_VOICE`、`VOLCANO_TTS_RESOURCE_ID`、`VOLCANO_TTS_CONTEXT`
- `config.py` AI 解说稿配置：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`

### 处理流水线（分块并发）

SSE 全量分析采用两阶段 + 分块并发：

| 阶段 | 操作 | 并发 | 耗时 |
|------|------|------|------|
| Phase 1 | Obsidian 文件搜索 + CSV 行情查询 | 串行（本地 IO） | <1s/条 |
| Phase 2 | DeepSeek API 生成解说稿 | 并发 3 条/批（`anyio.create_task_group`） | ~6s/批 |

```
Phase 1: 串行搜集上下文
  Item 0,1,2 → Obsidian + CSV (同步IO，但很快)

Phase 2: 分批并发调 AI
  批次1: [Item0, Item1, Item2] → anyio 任务组 同时调 DeepSeek
  批次2: [Item3, Item4, Item5] → anyio 任务组 同时调 DeepSeek
  ...
```

**关键实现**：
- Phase 2 用 `anyio.create_task_group().start_soon()` 实现真正并发（`asyncio.gather` 在 FastAPI SSE 生成器内不生效）
- `generate_commentary_ai_direct()` 跳过文件 IO，只做纯 HTTP 调用
- 每批 3 条（`CHUNK_SIZE = 3`），30 条从 ~150s 降到 ~50s

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 前端页面 |
| `/api/health` | GET | 健康检查 |
| `/api/analyze-all?feishu_url=` | GET | SSE 流式全量分析 |
| `/api/refresh/commentary` | POST | 单件拍品解说稿重生成 |
| `/api/refresh/market` | POST | 单件拍品行情重查询 |
| `/api/tts` | POST | 文字转语音（火山引擎），参数 `{"text":"...","voice":"音色ID"}` |
| `/api/item-cover?code=` | GET | 根据拍品 ID 获取封面图 URL（从小茶书后台抓取） |

### 前端

- 固定飞书链接（隐藏），按钮居中
- SSE 逐条推送：等待中 → 分析中 → 展开/收起
- 展开内容：解说稿（红色左边框）+ 成交行情（含行情分析紫色卡片 + 记录表格）
- 停止分析按钮（红色，AbortController）
- 解说稿和行情板块各自有独立「更新」按钮

### 已移除的旧模块

- ~~`modules/wechat_search.py`~~ — 搜狗微信搜索 → 改为 Obsidian
- ~~`modules/xiaochashu_search.py`~~ — 小茶书后台爬虫 → 改为 CSV
- ~~`modules/xianyu_search.py`~~ — 闲鱼搜索（整个业务线已移除）
- ~~`modules/link_verifier.py`~~ — 闲鱼链接验证（随闲鱼一起移除）

## 拍品名拆解规则

`utils/text_parser.py` 从拍品名中拆解 4 要素，用于搜索匹配和 match_type 标记：

| 要素 | 示例（宣之堂青花暗刻事事如意纹马蹄杯） | match_type | 搜索目录 |
|------|------|------|------|
| 窑口 | 宣之堂 | `kiln` / `kiln_analysis` | `wiki/窑口/` |
| 工艺 | 青花暗刻 | `kiln_craft` | `wiki/工艺/` + `wiki/釉色/` |
| 纹饰/题材 | 事事如意纹 | `kiln_motif` | `wiki/题材/` |
| 器型 | 马蹄杯 | `kiln_vessel` | `wiki/器型/` |

**强制规则**：窑口必须匹配；分类型搜索，结果标记 `match_type`。

## 飞书字段映射

`modules/feishu_reader.py` 中的 `FIELD_LABEL_MAP` 负责将飞书文档字段标签映射到标准化字段名：

| 飞书标签（部分） | 标准化字段 |
|------|------|
| 拍品名字/拍品名称/品名 | `name` |
| 窑口/窑口名称/所属窑口 | `kiln` |
| 起拍价/起拍/起拍价格 | `starting_price` |
| 估价/参考价/预估价 | `estimate` |
| 年代/朝代/时期 | `era` |
| 材质/质地/胎质 | `material` |
| 尺寸/规格/大小 | `size` |
| 拍品描述/描述/详情 | `description` |

读取策略（按优先级）：DOM 结构提取 → 表格解析 → 全文正则兜底。

## 解说稿结构

`main.py: generate_commentary()` 按 4 段生成约 300-400 字解说稿：

1. **开场**：拍品名称 + 窑口 + 年代 + 材质 + 尺寸 + 品相（来自飞书参数）
2. **窑口背景**：创始人/历史/核心特色/市场地位（来自 Obsidian `kiln_analysis`）
3. **器型+画片+工艺**：器型描述 + 纹饰题材 + 工艺技法（来自 Obsidian wiki 子目录）
4. **市场参考**：飞书估价+起拍价 + 成交行情均价区间（来自 CSV） + 竞价引导

## SSE 事件类型

`GET /api/analyze-all` 推送以下事件：

| 事件 | 触发时机 | 携带数据 |
|------|------|------|
| `start` | 分析开始 | `total`（总数）, `items`（摘要列表） |
| `progress` | 每条拍品开始处理 | `index`, `status: "searching"` |
| `result` | 单条拍品完成 | 解说稿/行情分析/成交记录/窑口链接 |
| `done` | 全部完成 | — |
| `error` | 出错 | `message` |

## 前端状态流转

表格状态列生命周期：`等待中`(灰) → `分析中`(黄, 脉冲动画) → `展开`(蓝按钮) ⇄ `收起`(蓝实心)

展开后区块顺序：拍品图横向滚动画廊 → 解说稿（红色左边框） → 成交行情（行情分析紫色卡片 + 记录表格）

两个板块各有一个右浮动蓝色「更新」按钮，独立触发 `/api/refresh/commentary` 或 `/api/refresh/market`。

表格列宽：`#`(30px) | `拍品名字`(42%) | `窑口`(14%) | `起拍价`(14%) | `参考价`(14%) | `状态`(100px)，`table-layout: fixed`

点击表格整行可展开/收起拍品（非仅按钮）。

## 语音合成（TTS）

火山引擎豆包语音 v3 单向流式 API，将解说稿转为 MP3 音频并在线播放/下载。

### API 信息

| 项目 | 值 |
|------|------|
| 端点 | `https://openspeech.bytedance.com/api/v3/tts/unidirectional` |
| 鉴权方式 | `X-Api-Key` + `X-Api-Resource-Id`（非 Bearer token） |
| 响应格式 | 多行 JSON（每行 `{"code":0,"data":"base64..."}`） |
| 文档 | https://www.volcengine.com/docs/6561/1329505?lang=zh |

### 鉴权 Headers

```
X-Api-Key: {VOLCANO_TTS_TOKEN}
X-Api-Resource-Id: seed-tts-2.0 | seed-icl-2.0
X-Api-Request-Id: <uuid>
Content-Type: application/json
```

### 请求体

```json
{
    "user": {"uid": "auction_user"},
    "req_params": {
        "text": "解说稿文本",
        "speaker": "<音色ID>",
        "audio_params": {"format": "mp3", "sample_rate": 24000},
        "additions": "{\"context_texts\": \"{VOLCANO_TTS_CONTEXT}\"}"
    }
}
```

### 音色配置（config.py）

| 音色 | speaker ID | Resource ID | 类型 |
|------|------|------|------|
| AI音色（默认） | `zh_male_wennuanahu_uranus_bigtts` | `seed-tts-2.0` | 系统音色 |
| 志波 | `S_2fqs4pf62` | `seed-icl-2.0` | 声音复刻 |
| 锦卉 | `S_1fqs4pf62` | `seed-icl-2.0` | 声音复刻 |
| 杨雪 | `S_0fqs4pf62` | `seed-icl-2.0` | 声音复刻 |
| 培文 | `S_Zeqs4pf62` | `seed-icl-2.0` | 声音复刻 |

**关键规则**：
- 系统音色用 `seed-tts-2.0`，复刻音色（`S_` 前缀）用 `seed-icl-2.0`
- `additions.context_texts` 为 JSON 字符串，设置播报上下文风格
- 响应中 `code=20000000` 为结束标记，`code=0` 为数据块，`code=55000000` 表示 resource/speaker 不匹配
- 音频数据在 `data` 字段，base64 解码后拼接得到完整 MP3

### 前端交互

| 功能 | 说明 |
|------|------|
| 音色选择 | 下拉框切换，生成时传入对应 speaker ID |
| 生成语音 | 底部全宽按钮，调用 `/api/tts` |
| 播放控制 | ▶/⏸ + 可拖拽进度条 + 时间显示 |
| 下载音频 | ⬇ 按钮，文件名格式 `{序号}_{拍品名}_{日期}_{音色名}.mp3` |
| 字幕同步 | 按句子字数比例分配时间，单行居中显示当前句 |
| 编辑解说稿 | 编辑后保存，自动重置音频状态 |
| 重新生成 | 🔄 按钮，停止当前播放后重新生成 |

## 快照缓存

分析完成后自动保存全量结果到 `cache/snapshot.json`，下次分析时对比指纹决定是否复用。

| 项目 | 值 |
|------|------|
| 缓存目录 | `action/cache/` |
| 结果文件 | `cache/snapshot.json`（含全部文本数据） |
| 音频目录 | `cache/audio/`（每个 index 只存最新生成的 mp3） |
| 锁文件 | `cache/analysis.lock`（5 分钟自动过期） |
| 指纹计算 | `code:name:kiln:starting_price:estimate` 的 MD5 |

### 分析流程

```
点击分析 → 获取锁 → 飞书 API → 计算指纹
    ├─ 指纹相同 → 直接推送缓存（秒开）
    └─ 指纹不同 → 全量重处理 → 覆盖缓存 + 清空旧音频
释放锁
```

### 并发控制

- 分析前获取文件锁，已有锁则返回 "当前有任务分析中，请稍后重试"
- 客户端断开或异常时自动释放锁
- 锁超过 5 分钟自动过期（防止死锁）

## 封面图获取

通过飞书拍品的 `code`（编号/ID）字段，拼接小茶书后台地址抓取所有拍品图。

| 项目 | 值 |
|------|------|
| 详情页地址 | `https://manage.funsun.cn/admin/v320sendproductinfo/edit/?id={code}` |
| 登录地址 | `https://manage.funsun.cn/login/` |
| 账号/密码 | 环境变量 `FUNSUN_EMAIL` / `FUNSUN_PASSWORD` |
| 登录态 | Playwright `storage_state` 持久化到 `funsun_state.json` |
| API | `GET /api/item-cover?code={code}` → `{"code":"...","images":["url1","url2",...]}` |
| 前端展示 | 横向滚动画廊（100x100 缩略图），点击查看大图，多图时滚动条常驻 |

## 成交行情

CSV 数据源：`raw/数据/成交行情.csv`（编码 `utf-8-sig`，注意 BOM）

| 项目 | 值 |
|------|------|
| CSV 列 | `id, goods_name, start_price, deal_price, auction_time, source, is_hide` |
| 查看原文 | `https://manage.funsun.cn/admin/newquotationdealinfo/?flt1_1={记录ID}` |

## 数据流（更新后）

```
飞书多维表格
  ├─ code 字段（编号/ID）
  ├─→ funsun.cn 详情页 → 所有拍品图（横向滚动画廊）
  └─→ Obsidian wiki/ → AI 解说稿 → TTS 语音 + 字幕
成交行情 CSV
  ├─ id 字段
  └─→ funsun.cn 查看原文链接
```
