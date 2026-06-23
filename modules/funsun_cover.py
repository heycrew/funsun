"""
小茶书管理后台封面图获取模块
登录 → 按商品 ID 抓取封面图 URL
"""
import asyncio, logging, os, re
from dotenv import load_dotenv
load_dotenv()

from utils.browser import browser_manager

logger = logging.getLogger(__name__)

FUNSUN_EMAIL = os.getenv("FUNSUN_EMAIL")
FUNSUN_PASSWORD = os.getenv("FUNSUN_PASSWORD")
LOGIN_URL = "https://manage.funsun.cn/login/"
DETAIL_URL = "https://manage.funsun.cn/admin/v320sendproductinfo/edit/?id={code}"
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "funsun_state.json")

_ctx = None  # 持久化登录上下文


async def _get_context():
    """获取或创建已登录的浏览器上下文（复用登录态）"""
    global _ctx
    state_abs = os.path.abspath(STATE_PATH)

    # 尝试复用已有上下文
    if _ctx is not None:
        try:
            page = await _ctx.new_page()
            await page.goto("https://manage.funsun.cn/admin/", wait_until="domcontentloaded", timeout=10000)
            await asyncio.sleep(1)
            if "login" not in page.url.lower():
                await page.close()
                return _ctx
            await page.close()
        except Exception:
            pass

    # 尝试加载已保存的登录状态
    if os.path.exists(state_abs):
        try:
            _ctx = await browser_manager._browser.new_context(
                storage_state=state_abs,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = await _ctx.new_page()
            await page.goto("https://manage.funsun.cn/admin/", wait_until="domcontentloaded", timeout=10000)
            await asyncio.sleep(1)
            if "login" not in page.url.lower():
                logger.info("小茶书登录态复用成功")
                await page.close()
                return _ctx
            await page.close()
            await _ctx.close()
            _ctx = None
        except Exception:
            pass

    # 重新登录
    logger.info("小茶书登录中...")
    _ctx = await browser_manager._browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
    )
    page = await _ctx.new_page()
    page.set_default_timeout(15000)

    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.fill('input[name="email"]', FUNSUN_EMAIL)
        await page.fill('input[name="password"]', FUNSUN_PASSWORD)
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)

        if "login" in page.url.lower():
            logger.error("小茶书登录失败")
            await page.close()
            return None

        # 保存登录状态
        await _ctx.storage_state(path=state_abs)
        logger.info(f"小茶书登录成功，状态已保存: {state_abs}")
        await page.close()
        return _ctx
    except Exception as e:
        logger.error(f"小茶书登录异常: {e}")
        await page.close()
        return None


async def fetch_product_images(code: str) -> list[str]:
    """根据拍品 ID 获取详情页所有拍品图 URL"""
    if not code:
        return []

    ctx = await _get_context()
    if ctx is None:
        return []

    url = DETAIL_URL.format(code=code)
    logger.info(f"获取拍品图: code={code}")

    try:
        page = await ctx.new_page()
        page.set_default_timeout(15000)
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        html = await page.content()
        await page.close()

        imgs = re.findall(r'<img[^>]+src="([^"]+)"', html)
        # 过滤：排除 icon/logo/验证码/极小图，保留 pqn.funsun.cn 的图片
        product_imgs = []
        for img in imgs:
            low = img.lower()
            if any(kw in low for kw in ['icon', 'logo', 'captcha', 'verify']):
                continue
            if img.startswith("/"):
                img = "https://manage.funsun.cn" + img
            if 'funsun.cn' in img and len(img) > 40:
                product_imgs.append(img)

        # 去重保持顺序
        seen = set()
        result = []
        for img in product_imgs:
            if img not in seen:
                seen.add(img)
                result.append(img)

        logger.info(f"拍品图 code={code}: {len(result)} 张")
        return result

    except Exception as e:
        logger.error(f"拍品图获取失败 code={code}: {e}")
        return []


async def fetch_covers_batch(codes: list[str]) -> dict[str, str]:
    """
    批量获取封面图（共用同一个登录态）

    Returns:
        {code: image_url}
    """
    if not await _ensure_logged_in():
        return {}

    results = {}
    for code in codes:
        url = await fetch_cover_image(code)
        results[code] = url
    return results
