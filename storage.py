import json
import os
import random
import sqlite3
import zipfile
from pathlib import Path

BASE_DIR = Path("data")
STORAGE_DIR = BASE_DIR / "accounts"
TEMP_DIR = BASE_DIR / "temp"
DB_PATH = BASE_DIR / "panel.db"

FINGERPRINT_PRESETS = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": {"width": 1440, "height": 900},
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "platform": "Win32",
        "viewport": {"width": 1920, "height": 1080},
        "hardware_concurrency": 12,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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
                proxy TEXT,
                fingerprint_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _detect_locale_and_tz(meta: dict) -> tuple[str, str]:
    lang = str(meta.get("lang_pack") or meta.get("system_lang_code") or "en").lower()
    if lang.startswith("pl"):
        return "pl-PL", "Europe/Warsaw"
    if lang.startswith("uk"):
        return "uk-UA", "Europe/Kyiv"
    if lang.startswith("de"):
        return "de-DE", "Europe/Berlin"
    if lang.startswith("ru"):
        return "ru-RU", "Europe/Berlin"
    return "en-US", "America/New_York"


def build_fingerprint(meta: dict) -> str:
    preset = random.choice(FINGERPRINT_PRESETS)
    locale, timezone = _detect_locale_and_tz(meta)
    fp = {
        **preset,
        "locale": locale,
        "timezone": timezone,
    }
    return json.dumps(fp, ensure_ascii=False)


def set_proxy_pool(proxies: list[str]) -> int:
    clean = [p.strip() for p in proxies if p.strip()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM proxies")
        for p in clean:
            conn.execute("INSERT OR IGNORE INTO proxies (raw_proxy) VALUES (?)", (p,))
        # Распределяем пул по кругу на все аккаунты
        acc_rows = conn.execute("SELECT id FROM accounts ORDER BY id ASC").fetchall()
        if clean:
            for idx, (acc_id,) in enumerate(acc_rows):
                proxy_val = clean[idx % len(clean)]
                conn.execute("UPDATE accounts SET proxy = ? WHERE id = ?", (proxy_val, acc_id))
        else:
            conn.execute("UPDATE accounts SET proxy = NULL")
        conn.commit()
    return len(clean)


def _get_next_proxy_round_robin(conn: sqlite3.Connection, current_count: int) -> str | None:
    proxies = [r[0] for r in conn.execute("SELECT raw_proxy FROM proxies ORDER BY id ASC").fetchall()]
    if not proxies:
        return None
    return proxies[current_count % len(proxies)]


def import_zip_archive(zip_path: Path) -> dict:
    extract_dir = TEMP_DIR / f"unpack_{os.getpid()}_{random.randint(1000, 9999)}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    added = []
    skipped = []

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        sessions = {}
        jsons = {}

        for root, _, files in os.walk(extract_dir):
            for fname in files:
                full = Path(root) / fname
                if fname.endswith(".session"):
                    sessions[fname[:-8]] = full
                elif fname.endswith(".json"):
                    jsons[fname[:-5]] = full

        with sqlite3.connect(DB_PATH) as conn:
            existing_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

            for name, sess_file in sessions.items():
                json_file = jsons.get(name)
                if not json_file:
                    skipped.append(f"{name}: нет пары .json")
                    continue

                try:
                    meta = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    skipped.append(f"{name}: битый json ({exc})")
                    continue

                acc_dir = STORAGE_DIR / name
                acc_dir.mkdir(parents=True, exist_ok=True)
                target_sess = acc_dir / f"{name}.session"
                target_json = acc_dir / f"{name}.json"

                target_sess.write_bytes(sess_file.read_bytes())
                target_json.write_bytes(json_file.read_bytes())

                fp_json = build_fingerprint(meta)
                proxy_assigned = _get_next_proxy_round_robin(conn, existing_count + len(added))

                try:
                    conn.execute(
                        """
                        INSERT INTO accounts (name, session_path, json_path, proxy, fingerprint_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (name, str(target_sess), str(target_json), proxy_assigned, fp_json),
                    )
                    added.append(name)
                except sqlite3.IntegrityError:
                    # Обновим файлы и прокси, но сохраним старый фингерпринт
                    conn.execute(
                        """
                        UPDATE accounts
                        SET session_path = ?, json_path = ?, proxy = COALESCE(?, proxy)
                        WHERE name = ?
                        """,
                        (str(target_sess), str(target_json), proxy_assigned, name),
                    )
                    skipped.append(f"{name}: уже был в БД (обновлены файлы)")

            conn.commit()
    finally:
        # Чистим временную папку распаковки и архив
        for root, dirs, files in os.walk(extract_dir, topdown=False):
            for f in files:
                Path(root, f).unlink(missing_ok=True)
            for d in dirs:
                Path(root, d).rmdir()
        extract_dir.rmdir()
        zip_path.unlink(missing_ok=True)

    return {"added": added, "skipped": skipped}


def list_accounts(limit: int = 20) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, name, proxy, fingerprint_json FROM accounts ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()


def get_account(acc_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, name, session_path, json_path, proxy, fingerprint_json FROM accounts WHERE id = ?",
            (acc_id,),
        ).fetchone()
