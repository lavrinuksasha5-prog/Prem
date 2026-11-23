import hashlib
import json
import os
import random
import shutil
import sqlite3
import zipfile
from pathlib import Path

BASE_DIR = Path(os.getenv("DATA_DIR", "data"))
STORAGE_DIR = BASE_DIR / "accounts"
TEMP_DIR = BASE_DIR / "temp"
DB_PATH = BASE_DIR / "panel.db"

FINGERPRINT_PRESETS = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": {"width": 1440, "height": 900},
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": {"width": 1920, "height": 1080},
        "hardware_concurrency": 12,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "platform": "MacIntel",
        "viewport": {"width": 1512, "height": 982},
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple M2",
    },
]


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
                two_fa TEXT,
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


def build_fingerprint(acc_name: str, meta: dict) -> str:
    # Детерминированный выбор пресета по имени аккаунта + метаданным
    seed = int(hashlib.sha256(acc_name.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    preset = rng.choice(FINGERPRINT_PRESETS)
    locale, timezone = _detect_locale_and_tz(meta)
    fp = {
        **preset,
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
        conn.execute("UPDATE accounts SET proxy = ? WHERE id = ?", (proxy_val, acc_id))


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
            # Защита от Zip Slip (выход за пределы dest_dir через ../)
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"Небезопасный путь в архиве: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _scan_all_files(root_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Рекурсивно обходит вообще все папки и подпапки внутри распакованного архива."""
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

                two_fa = _extract_two_fa(meta)
                acc_dir = STORAGE_DIR / name
                acc_dir.mkdir(parents=True, exist_ok=True)

                target_sess = acc_dir / f"{name}.session"
                target_json = acc_dir / f"{name}.json"

                shutil.copyfile(sess_file, target_sess)
                shutil.copyfile(json_file, target_json)

                fp_json = build_fingerprint(name, meta)

                existing = conn.execute(
                    "SELECT id, fingerprint_json FROM accounts WHERE name = ?", (name,)
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO accounts (name, session_path, json_path, two_fa, proxy, fingerprint_json)
                        VALUES (?, ?, ?, ?, NULL, ?)
                        """,
                        (name, str(target_sess), str(target_json), two_fa, fp_json),
                    )
                    added.append(name)
                else:
                    # Сохраняем уже выданный фингерпринт, обновляем файлы и 2FA
                    conn.execute(
                        """
                        UPDATE accounts
                        SET session_path = ?, json_path = ?, two_fa = COALESCE(?, two_fa)
                        WHERE name = ?
                        """,
                        (str(target_sess), str(target_json), two_fa, name),
                    )
                    updated.append(name)

            # Перераспределяем пул прокси по всем аккаунтам по кругу
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
        return conn.execute(
            """
            SELECT id, name, proxy, fingerprint_json, two_fa
            FROM accounts
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def get_account(acc_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT id, name, session_path, json_path, two_fa, proxy, fingerprint_json
            FROM accounts
            WHERE id = ?
            """,
            (acc_id,),
        ).fetchone()
