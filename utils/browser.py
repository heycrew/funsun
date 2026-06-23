"""
Playwright 浏览器实例管理器
支持普通上下文和闲鱼持久化登录上下文
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import config

logger = logging.getLogger(__name__)


class BrowserManager:
    """浏览器管理器，全局单例"""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._xianyu_context: BrowserContext | None = None
        self._xianyu_logged_in = False
        self._xianyu_login_attempted = False  # 防止重复弹窗

    async def start(self):
        """启动浏览器"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.BROWSER_HEADLESS,
            slow_mo=config.BROWSER_SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

    async def stop(self):
        """关闭浏览器"""
        if self._xianyu_context:
            await self._xianyu_context.close()
            self._xianyu_context = None
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @asynccontextmanager
    async def new_context(self) -> BrowserContext:
        """创建新的浏览器上下文（隔离的cookie和session）"""
        if not self._browser:
            await self.start()

        context = await self._browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        try:
            yield context
        finally:
            await context.close()

    @asynccontextmanager
    async def new_page(self, context: BrowserContext = None) -> Page:
        """创建新页面，可指定上下文"""
        close_context = False
        if context is None:
            context = await self._browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            close_context = True

        page = await context.new_page()
        page.set_default_timeout(config.BROWSER_TIMEOUT)
        try:
            yield page
        finally:
            if close_context:
                await context.close()

    # ========== 闲鱼持久化登录 ==========

    async def _ensure_xianyu_logged_in(self):
        """
        确保闲鱼处于登录状态（服务端安全版，不阻塞）
        首次使用弹出可见浏览器窗口，自动等待扫码登录完成
        仅尝试一次，失败后不再弹出
        """
        if self._xianyu_logged_in and self._xianyu_context:
            return self._xianyu_context
        if self._xianyu_login_attempted:
            return self._xianyu_context

        profile_dir = os.path.abspath(config.XIANYU_PROFILE_DIR)
        os.makedirs(profile_dir, exist_ok=True)
        state_path = os.path.join(profile_dir, "state.json")

        # 尝试加载已保存的登录状态
        if os.path.exists(state_path):
            self._xianyu_context = await self._browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                storage_state=state_path,
            )
            page = await self._xianyu_context.new_page()
            await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            body = await page.inner_text("body")
            still_valid = not any(kw in body[:500] for kw in ["登录", "登陆", "扫码"])
            await page.close()

            if still_valid:
                logger.info("闲鱼已登录（复用持久化Session）")
                self._xianyu_logged_in = True
                return self._xianyu_context
            else:
                logger.info("登录状态已过期，需要重新登录")
                await self._xianyu_context.close()
                self._xianyu_context = None
                # 删除过期的 state.json
                if os.path.exists(state_path):
                    os.remove(state_path)

        # 需要登录 —— 弹出可见浏览器，只加载一次，静默检测
        logger.info("闲鱼未登录，正在打开登录窗口...")
        logger.info("请在弹出的浏览器窗口中扫码，60 秒内完成，请勿关闭窗口")

        was_headless = config.BROWSER_HEADLESS

        # 如果有旧的 state.json，先删除（确保干净的登录环境）
        if os.path.exists(state_path):
            os.remove(state_path)

        await self._browser.close()

        self._browser = await self._playwright.chromium.launch(
            headless=False,
            slow_mo=config.BROWSER_SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        self._xianyu_context = await self._browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )

        login_page = await self._xianyu_context.new_page()

        # 打开闲鱼首页（只加载一次！）
        await login_page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=20000)
        initial_url = login_page.url
        logger.info(f"当前页面: {initial_url[:100]}")

        # 如果已经在登录页（passport），等待扫码后自动跳转
        # 如果在首页但需要登录，闲鱼会弹 modal
        is_passport = "passport" in initial_url

        # 静默轮询（不刷新页面！只读当前状态）
        login_success = False
        for i in range(60):
            await asyncio.sleep(1)
            try:
                current_url = login_page.url
                body = await login_page.inner_text("body")

                # === 登录成功判断（必须同时满足） ===
                # 正信号：页面出现用户相关的功能入口（登录后才有的元素）
                positive_signals = ["发布", "消息", "我的", "关注", "我发布的",
                                   "收藏", "浏览记录", "卖出的", "买到的", "评价"]
                has_user_menu = any(kw in body[:800] for kw in positive_signals)
                # 负信号：页面还有登录相关提示
                login_prompts = ["请登录", "扫码登录", "手机登录", "密码登录", "短信登录",
                                "登录/注册", "登录/注冊", "短信验证码", "请输入手机号",
                                "手机号登录", "淘宝账号登录", "支付宝登录"]
                has_login_prompt = any(kw in body[:800] for kw in login_prompts)
                # URL 信号：是否跳转到 passport 登录页
                on_passport = "passport" in current_url or "login.htm" in current_url

                # 10 秒后才开始检测（给用户扫码时间）
                if i >= 10:
                    if has_user_menu and not has_login_prompt and not on_passport:
                        login_success = True
                        logger.info(f"检测到登录成功！({i}秒)")
                        break

                # 如果是 passport 页面且 URL 已跳回 goofish.com → 登录成功
                if is_passport and i >= 5:
                    if not on_passport and has_user_menu:
                        login_success = True
                        logger.info(f"登录页跳转成功！({i}秒)")
                        break

                if i % 10 == 0:
                    if is_passport:
                        logger.info(f"等待扫码登录... ({i}s/60s)")
                    else:
                        logger.info(f"等待扫码登录（首页弹窗模式）... ({i}s/60s)")

            except Exception as e:
                logger.debug(f"检测异常: {e}")

        # 登录成功后等一下确保 Cookie 写入
        if login_success:
            await asyncio.sleep(2)

        await login_page.close()

        if login_success:
            self._xianyu_logged_in = True
            await self._xianyu_context.storage_state(path=state_path)
            logger.info(f"登录状态已保存到: {state_path}")
        else:
            logger.warning("登录超时(60s)，将以未登录状态继续")
            self._xianyu_logged_in = False
            self._xianyu_login_attempted = True

        # 关闭登录窗口的 context 和 browser
        if self._xianyu_context:
            await self._xianyu_context.close()
            self._xianyu_context = None
        await self._browser.close()

        # 重新启动 browser（恢复原来的 headless 模式）
        self._browser = await self._playwright.chromium.launch(
            headless=was_headless,
            slow_mo=config.BROWSER_SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        return self._xianyu_context

    async def get_xianyu_context(self) -> BrowserContext:
        """
        获取闲鱼登录上下文
        - 已登录：直接返回
        - 登录过期/未登录：弹出浏览器窗口扫码登录
        """
        if self._xianyu_logged_in and self._xianyu_context:
            return self._xianyu_context

        profile_dir = os.path.abspath(config.XIANYU_PROFILE_DIR)
        os.makedirs(profile_dir, exist_ok=True)
        state_path = os.path.join(profile_dir, "state.json")

        # 尝试加载已保存的登录状态
        if os.path.exists(state_path):
            try:
                self._xianyu_context = await self._browser.new_context(
                    user_agent=config.USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                    storage_state=state_path,
                )
                page = await self._xianyu_context.new_page()
                await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                body = await page.inner_text("body")
                await page.close()

                if not any(kw in body[:500] for kw in ["登录", "登陆", "扫码"]):
                    self._xianyu_logged_in = True
                    logger.info("闲鱼登录态已加载")
                    return self._xianyu_context
                else:
                    logger.info("闲鱼登录态已过期，重新登录...")
                    await self._xianyu_context.close()
                    self._xianyu_context = None
            except Exception as e:
                logger.warning(f"加载闲鱼登录态失败: {e}")

        # 需要登录：弹出可见浏览器
        logger.info("正在打开闲鱼登录窗口，60 秒内扫码...")
        was_headless = config.BROWSER_HEADLESS

        # 关闭旧浏览器，用可见模式打开
        if self._xianyu_context:
            await self._xianyu_context.close()
            self._xianyu_context = None
        await self._browser.close()

        self._browser = await self._playwright.chromium.launch(
            headless=False,
            slow_mo=config.BROWSER_SLOW_MO,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        self._xianyu_context = await self._browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )

        login_page = await self._xianyu_context.new_page()
        await login_page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=20000)

        # 静默轮询检测登录（不刷新页面！）
        login_success = False
        for i in range(60):
            await asyncio.sleep(1)
            try:
                body = await login_page.inner_text("body")
                current_url = login_page.url

                # 正信号 + 负信号判断
                has_menu = any(kw in body[:800] for kw in ["发布", "消息", "我的", "关注"])
                no_prompt = not any(kw in body[:800] for kw in ["请登录", "扫码登录", "手机登录", "密码登录"])
                not_passport = "passport" not in current_url

                if i >= 10 and has_menu and no_prompt and not_passport:
                    login_success = True
                    logger.info(f"登录成功({i}秒)")
                    break
                if i % 15 == 0:
                    logger.info(f"等待扫码... ({i}s/60s)")
            except:
                pass

        await login_page.close()

        if login_success:
            await asyncio.sleep(1)
            await self._xianyu_context.storage_state(path=state_path)
            self._xianyu_logged_in = True
            logger.info(f"登录状态已保存: {state_path}")
        else:
            logger.warning("登录超时(60s)，本次跳过闲鱼搜索")
            self._xianyu_logged_in = False
            await self._xianyu_context.close()
            self._xianyu_context = None

        # 恢复 headless 模式，重建浏览器
        await self._browser.close()
        self._browser = await self._playwright.chromium.launch(
            headless=was_headless,
            slow_mo=config.BROWSER_SLOW_MO,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        # 登录成功后重建 context
        if login_success and os.path.exists(state_path):
            self._xianyu_context = await self._browser.new_context(
                user_agent=config.USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                storage_state=state_path,
            )
        else:
            self._xianyu_context = None

        return self._xianyu_context

    async def get_page_content(self, url: str, wait_selector: str = None, timeout: int = None) -> tuple[str, str]:
        """打开URL并获取页面内容"""
        timeout = timeout or config.BROWSER_TIMEOUT
        async with self.new_page() as page:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                if wait_selector:
                    await page.wait_for_selector(wait_selector, timeout=timeout)
                await asyncio.sleep(2)
                title = await page.title()
                content = await page.inner_text("body")
                return content, title
            except Exception as e:
                raise RuntimeError(f"无法访问页面 {url}: {str(e)}")


# 全局浏览器管理器实例
browser_manager = BrowserManager()
