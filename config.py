"""
拍品信息聚合系统 - 全局配置
"""
import os
from dotenv import load_dotenv

load_dotenv()

# 浏览器配置
BROWSER_HEADLESS = True          # 是否无头模式运行
BROWSER_TIMEOUT = 30000          # 页面加载超时（毫秒）
BROWSER_SLOW_MO = 0              # 操作间隔（毫秒），反爬时可设50-100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Obsidian vault 配置
OBSIDIAN_VAULT_PATH = r"D:\WorkDoc\当代茶艺瓷器"   # Obsidian vault 根目录
OBSIDIAN_VAULT_NAME = "当代茶艺瓷器"               # vault 名称（用于 obsidian:// 链接）
OBSIDIAN_MARKET_CSV = "raw/数据/成交行情.csv"       # 成交行情 CSV（相对于 vault 根目录）

# 搜索配置
OBSIDIAN_SEARCH_MAX_RESULTS = 5   # Obsidian 搜索每轮最大结果数

# DeepSeek API 配置（AI 解说稿生成）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

# 火山引擎 TTS 配置（doubao-tts2.0 / seed-tts-2.0）
VOLCANO_TTS_TOKEN = os.getenv("VOLCANO_TTS_TOKEN")
VOLCANO_TTS_VOICE = os.getenv("VOLCANO_TTS_VOICE", "zh_male_wennuanahu_uranus_bigtts")
VOLCANO_TTS_RESOURCE_ID = os.getenv("VOLCANO_TTS_RESOURCE_ID", "seed-tts-2.0")
VOLCANO_TTS_CONTEXT = os.getenv("VOLCANO_TTS_CONTEXT", "")

# 服务配置
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
