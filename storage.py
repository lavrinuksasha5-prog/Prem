import base64
import hashlib
import json
import os
import random
import shutil
import sqlite3
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(os.getenv("DATA_DIR", "data"))
STORAGE_DIR = BASE_DIR / "accounts"
TEMP_DIR = BASE_DIR / "temp"
DB_PATH = BASE_DIR / "panel.db"

USER_AGENTS_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "MacIntel",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "MacIntel",
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Linux x86_64",
    ),
]

WEBGL_POOL = [
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    ("Apple Inc.", "Apple M1"),
    ("Apple Inc.", "Apple M2"),
    ("Apple Inc.", "Apple M3"),
]

VIEWPORTS_POOL = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
    (1280, 720),
]

CPU_CORES_POOL = [4, 6, 8, 12, 16]
RAM_GB_POOL = [4, 8, 16, 32]


def _get_fernet() -> Fernet:
    raw_key = os.getenv("ENCRYPTION_KEY", "").strip()
    if raw_key:
        try:
            return Fernet(raw_key.encode("utf-8"))
        except Exception:
            pass
    # Фолбэк: детерминированный ключ из BOT_TOKEN (для продакшена задайте отдельный ENCRYPTION_KEY)
    token_seed = (os.getenv("BOT_TOKEN") or "default-panel-secret").encode("utf-8")
    digest = hashlib.sha256(token_seed).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value: str | None) -> str | None:
    if not value:
        return None
    f = _get_fernet()
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(token_str: str | None) -> str | None:
    if not token_str:
        return None
    f = _get_fernet()
    try:
        return f.decrypt(token_str.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # На случай старых незашифрованных записей
        return token_str


def init_storage():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_proxy TEXT UNIQUE NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                session_path TEXT NOT NULL,
                json_path TEXT NOT NULL,
                two_fa_enc TEXT,
                proxy TEXT,
                fingerprint_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _detect_locale_and_tz(meta: dict) -> tuple[str, str]:
    lang = str(
        meta.get("lang_pack")
        or meta.get("system_lang_code")
        or meta.get("lang_code")
        or "en"
    ).lower()
    if lang.startswith("pl"):
        return "pl-PL", "Europe/Warsaw"
    if lang.startswith("uk"):
        return "uk-UA", "Europe/Kyiv"
    if lang.startswith("de"):
        return "de-DE", "Europe/Berlin"
    if lang.startswith("ru"):
        return "ru-RU", "Europe/Berlin"
    return "en-US", "America/New_York"


def _extract_two_fa(meta: dict) -> str | None:
    for key in ("twoFA", "two_fa", "password", "2fa", "cloud_password"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def build_randomized_fingerprint(meta: dict) -> str:
    # Полностью случайная генерация при импорте, но сохраняется в БД навсегда на аккаунт
    ua, platform = random.choice(USER_AGENTS_POOL)
    gl_vendor, gl_renderer = random.choice(WEBGL_POOL)
    base_w, base_h = random.choice(VIEWPORTS_POOL)

    width = max(1024, base_w + random.choice([-40, -20, 0, 0, 20, 40]))
    height = max(640, base_h + random.choice([-30, -16, 0, 0, 16, 30]))

    locale, timezone = _detect_locale_and_tz(meta)
    fp = {
        "user_agent": ua,
        "platform": platform,
        "viewport": {"width": width, "height": height},
        "hardware_concurrency": random.choice(CPU_CORES_POOL),
        "device_memory": random.choice(RAM_GB_POOL),
        "webgl_vendor": gl_vendor,
        "webgl_renderer": gl_renderer,
        "canvas_noise_seed": random.randint(100000, 999999999),
        "audio_noise_seed": random.randint(100000, 999999999),
        "locale": locale,
        "timezone": timezone,
    }
    return json.dumps(fp, ensure_ascii=False)


def _reassign_proxies_locked(conn: sqlite3.Connection):
    proxies = [r[0] for r in conn.execute("SELECT raw_proxy FROM proxies ORDER BY id ASC").fetchall()]
    acc_rows = conn.execute("SELECT id FROM accounts ORDER BY id ASC").fetchall()
    if not acc_rows:
        return
    if not proxies:
        conn.execute("UPDATE accounts SET proxy = NULL")
        return
    for idx, (acc_id,) in enumerate(acc_rows):
        proxy_val = proxies[idx % len(proxies)]
        conn.execute("UPDATE accounts SET proxy = ? WHERE id = ?", (encrypt_value(proxy_val), acc_id))


def set_proxy_pool(proxies: list[str]) -> int:
    clean = [p.strip() for p in proxies if p.strip()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM proxies")
        for p in clean:
            conn.execute("INSERT OR IGNORE INTO proxies (raw_proxy) VALUES (?)", (p,))
        _reassign_proxies_locked(conn)
        conn.commit()
    return len(clean)


def _safe_extract_zip(zip_path: Path, dest_dir: Path):
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (dest_resolved / member.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"Небезопасный путь в архиве: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _scan_all_files(root_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    sessions: dict[str, Path] = {}
    jsons: dict[str, Path] = {}

    for current_root, _, files in os.walk(root_dir):
        for fname in files:
            full_path = Path(current_root) / fname
            low = fname.lower()
            if low.endswith(".session"):
                stem = fname[:-8]
                if stem:
                    sessions[stem] = full_path
            elif low.endswith(".json"):
                stem = fname[:-5]
                if stem:
                    jsons[stem] = full_path

    return sessions, jsons


def import_zip_archive(zip_path: Path) -> dict:
    extract_dir = TEMP_DIR / f"unpack_{os.getpid()}_{random.randint(10000, 99999)}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    try:
        _safe_extract_zip(zip_path, extract_dir)
        sessions, jsons = _scan_all_files(extract_dir)

        with sqlite3.connect(DB_PATH) as conn:
            for name, sess_file in sorted(sessions.items()):
                json_file = jsons.get(name)
                if not json_file:
                    skipped.append(f"{name}: не найден парный .json")
                    continue

                try:
                    meta = json.loads(json_file.read_text(encoding="utf-8"))
                    if not isinstance(meta, dict):
                        raise ValueError("JSON должен быть объектом")
                except Exception as exc:
                    skipped.append(f"{name}: ошибка чтения .json ({exc})")
                    continue

                two_fa_enc = encrypt_value(_extract_two_fa(meta))
                acc_dir = STORAGE_DIR / name
                acc_dir.mkdir(parents=True, exist_ok=True)

                target_sess = acc_dir / f"{name}.session"
                target_json = acc_dir / f"{name}.json"

                shutil.copyfile(sess_file, target_sess)
                shutil.copyfile(json_file, target_json)

                existing = conn.execute(
                    "SELECT id, fingerprint_json FROM accounts WHERE name = ?", (name,)
                ).fetchone()

                if existing is None:
                    fp_json = build_randomized_fingerprint(meta)
                    conn.execute(
                        """
                        INSERT INTO accounts (name, session_path, json_path, two_fa_enc, proxy, fingerprint_json)
                        VALUES (?, ?, ?, ?, NULL, ?)
                        """,
                        (name, str(target_sess), str(target_json), two_fa_enc, fp_json),
                    )
                    added.append(name)
                else:
                    conn.execute(
                        """
                        UPDATE accounts
                        SET session_path = ?, json_path = ?, two_fa_enc = COALESCE(?, two_fa_enc)
                        WHERE name = ?
                        """,
                        (str(target_sess), str(target_json), two_fa_enc, name),
                    )
                    updated.append(name)

            _reassign_proxies_locked(conn)
            conn.commit()
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)

    return {"added": added, "updated": updated, "skipped": skipped}


def count_accounts() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def list_accounts_page(offset: int = 0, limit: int = 6) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, name, proxy, fingerprint_json, two_fa_enc
            FROM accounts
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    result = []
    for acc_id, name, proxy_enc, fp_raw, two_fa_enc in rows:
        result.append(
            (
                acc_id,
                name,
                decrypt_value(proxy_enc),
                fp_raw,
                decrypt_value(two_fa_enc),
            )
        )
    return result


def get_account(acc_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, name, session_path, json_path, two_fa_enc, proxy, fingerprint_json
            FROM accounts
            WHERE id = ?
            """,
            (acc_id,),
        ).fetchone()
    if not row:
        return None
    acc_id, name, sess_path, json_path, two_fa_enc, proxy_enc, fp_raw = row
    return (
        acc_id,
        name,
        sess_path,
        json_path,
        decrypt_value(two_fa_enc),
        decrypt_value(proxy_enc),
        fp_raw,
    )
