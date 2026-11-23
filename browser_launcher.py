import asyncio
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from session_injector import build_local_storage_payload, extract_session_auth

PROFILES_DIR = Path(os.getenv("DATA_DIR", "data")) / "browser_profiles"
DISPLAY = os.getenv("DISPLAY", ":99")
VNC_PORT = int(os.getenv("VNC_PORT", "5900"))
NOVNC_WEB_PORT = int(os.getenv("NOVNC_WEB_PORT", "6080"))
NOVNC_PUBLIC_URL = os.getenv("NOVNC_PUBLIC_URL", "").strip()
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()

_vnc_started = False
_active_contexts: dict[str, tuple] = {}


def get_novnc_url() -> str | None:
    if NOVNC_PUBLIC_URL:
        base = NOVNC_PUBLIC_URL.rstrip("/")
        return f"{base}/vnc.html?autoconnect=1&resize=scale"
    if RAILWAY_DOMAIN:
        return f"https://{RAILWAY_DOMAIN}/vnc.html?autoconnect=1&resize=scale"
    return None


def _find_novnc_root() -> str | None:
    candidates = [
        "/usr/share/novnc",
        "/usr/local/lib/python3.10/dist-packages/novnc",
        "/opt/noVNC",
    ]
    for path in candidates:
        if Path(path).exists() and (Path(path) / "vnc.html").exists():
            return path
    return None


def start_vnc_stack_on_boot() -> int:
    global _vnc_started
    listen_port = int(os.getenv("PORT") or NOVNC_WEB_PORT)
    if _vnc_started:
        return listen_port

    # 1. Виртуальный экран Xvfb
    subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1366x820x24", "-ac"],
    )

    # 2. VNC сервер поверх Xvfb
    subprocess.Popen(
        [
            "x11vnc",
            "-display",
            DISPLAY,
            "-nopw",
            "-forever",
            "-shared",
            "-rfbport",
            str(VNC_PORT),
        ],
    )

    # 3. noVNC через websockify на порту Railway (PORT)
    novnc_root = _find_novnc_root()
    if novnc_root:
        subprocess.Popen(
            [
                "websockify",
                f"--web={novnc_root}",
                str(listen_port),
                f"127.0.0.1:{VNC_PORT}",
            ],
        )
        print(f"[VNC] websockify запущен на порту {listen_port} (web={novnc_root}, display={DISPLAY})")
    else:
        print(f"[VNC] ВНИМАНИЕ: директория noVNC не найдена, запускаю websockify без --web на порту {listen_port}")
        subprocess.Popen(
            [
                "websockify",
                str(listen_port),
                f"127.0.0.1:{VNC_PORT}",
            ],
        )

    _vnc_started = True
    return listen_port


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


def _build_stealth_init_script(fp: dict, ls_payload: dict[str, str] | None) -> str:
    vendor = json.dumps(fp.get("webgl_vendor", "Google Inc. (NVIDIA)"))
    renderer = json.dumps(
        fp.get(
            "webgl_renderer",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        )
    )
    hw_concurrency = int(fp.get("hardware_concurrency", 8))
    dev_memory = int(fp.get("device_memory", 8))
    canvas_seed = int(fp.get("canvas_noise_seed", 123456))
    audio_seed = int(fp.get("audio_noise_seed", 654321))
    ls_json = json.dumps(ls_payload or {})

    return f"""
    (() => {{
        const cSeed = {canvas_seed};
        const aSeed = {audio_seed};
        const authItems = {ls_json};

        if (authItems && Object.keys(authItems).length > 0) {{
            try {{
                if (!window.localStorage.getItem('user_auth')) {{
                    for (const [k, v] of Object.entries(authItems)) {{
                        window.localStorage.setItem(k, v);
                    }}
                }}
            }} catch (e) {{}}
        }}

        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hw_concurrency} }});
        Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {dev_memory} }});

        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return {vendor};
            if (param === 37446) return {renderer};
            return getParam.call(this, param);
        }};

        if (window.WebGL2RenderingContext) {{
            const getParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {{
                if (param === 37445) return {vendor};
                if (param === 37446) return {renderer};
                return getParam2.call(this, param);
            }};
        }}

        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(sx, sy, sw, sh) {{
            const imgData = origGetImageData.call(this, sx, sy, sw, sh);
            const data = imgData.data;
            for (let i = 0; i < data.length; i += 400) {{
                const delta = ((cSeed + i) % 3) - 1;
                data[i] = Math.max(0, Math.min(255, data[i] + delta));
            }}
            return imgData;
        }};

        if (window.AudioBuffer && AudioBuffer.prototype.getChannelData) {{
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {{
                const arr = origGetChannelData.call(this, channel);
                for (let i = 0; i < arr.length; i += 1024) {{
                    arr[i] = arr[i] + (((aSeed + i) % 5) - 2) * 1e-7;
                }}
                return arr;
            }};
        }}
    }})();
    """


async def launch_web_telegram(
    acc_name: str,
    raw_proxy: str | None,
    fp: dict,
    session_path: str | None = None,
    json_path: str | None = None,
) -> tuple[str | None, bool]:
    start_vnc_stack_on_boot()

    ls_payload: dict[str, str] | None = None
    injected_ok = False
    if session_path:
        try:
            auth_data = await extract_session_auth(
                session_path=session_path,
                json_path=json_path,
                raw_proxy=raw_proxy,
            )
            ls_payload = build_local_storage_payload(auth_data)
            injected_ok = True
        except Exception as exc:
            print(f"[INJECT] Ошибка извлечения сессии для {acc_name}: {exc}")
            injected_ok = False

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir = PROFILES_DIR / acc_name
    user_data_dir.mkdir(parents=True, exist_ok=True)

    prev = _active_contexts.pop(acc_name, None)
    if prev:
        old_ctx, old_pw = prev
        try:
            await old_ctx.close()
            await old_pw.stop()
        except Exception:
            pass

    proxy_cfg = _parse_proxy(raw_proxy)
    viewport = fp.get("viewport") or {"width": 1366, "height": 768}

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=False,
        env=env,
        proxy=proxy_cfg,
        user_agent=fp.get("user_agent"),
        locale=fp.get("locale", "en-US"),
        timezone_id=fp.get("timezone", "America/New_York"),
        viewport=viewport,
        args=[
            f"--display={DISPLAY}",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    await context.add_init_script(_build_stealth_init_script(fp, ls_payload))
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded")

    _active_contexts[acc_name] = (context, playwright)
    return get_novnc_url(), injected_ok
