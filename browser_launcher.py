import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

PROFILES_DIR = Path(os.getenv("DATA_DIR", "data")) / "browser_profiles"
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"


def _parse_proxy(raw_proxy: str | None) -> dict | None:
    if not raw_proxy:
        return None
    val = raw_proxy.strip()
    if "://" not in val:
        val = f"http://{val}"
    parsed = urlparse(val)
    if not parsed.hostname or not parsed.port:
        return None
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _build_stealth_init_script(fp: dict) -> str:
    vendor = json.dumps(fp.get("webgl_vendor", "Google Inc. (NVIDIA)"))
    renderer = json.dumps(
        fp.get(
            "webgl_renderer",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        )
    )
    hw_concurrency = int(fp.get("hardware_concurrency", 8))
    dev_memory = int(fp.get("device_memory", 8))

    return f"""
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hw_concurrency} }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {dev_memory} }});

    const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        if (param === 37445) return {vendor};
        if (param === 37446) return {renderer};
        return getParameterOrig.call(this, param);
    }};

    if (window.WebGL2RenderingContext) {{
        const getParameter2Orig = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return {vendor};
            if (param === 37446) return {renderer};
            return getParameter2Orig.call(this, param);
        }};
    }}
    """


async def launch_web_telegram(acc_name: str, raw_proxy: str | None, fp: dict):
    """Запускает изолированный профиль браузера для аккаунта с прокси и фингерпринтом."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir = PROFILES_DIR / acc_name
    user_data_dir.mkdir(parents=True, exist_ok=True)

    proxy_cfg = _parse_proxy(raw_proxy)
    viewport = fp.get("viewport") or {"width": 1440, "height": 900}

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=HEADLESS,
        proxy=proxy_cfg,
        user_agent=fp.get("user_agent"),
        locale=fp.get("locale", "en-US"),
        timezone_id=fp.get("timezone", "America/New_York"),
        viewport=viewport,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    await context.add_init_script(_build_stealth_init_script(fp))
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded")

    # Держим контекст живым в фоновой задаче
    async def _keep_alive():
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await context.close()
            await playwright.stop()

    asyncio.create_task(_keep_alive())
