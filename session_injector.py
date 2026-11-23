import asyncio
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession


def _parse_telethon_proxy(raw_proxy: str | None) -> tuple | None:
    if not raw_proxy:
        return None
    val = raw_proxy.strip()
    if "://" not in val:
        val = f"http://{val}"
    parsed = urlparse(val)
    if not parsed.hostname or not parsed.port:
        return None

    import python_socks

    scheme = parsed.scheme.lower()
    if scheme.startswith("socks5"):
        ptype = python_socks.ProxyType.SOCKS5
    elif scheme.startswith("socks4"):
        ptype = python_socks.ProxyType.SOCKS4
    else:
        ptype = python_socks.ProxyType.HTTP

    return (
        ptype,
        parsed.hostname,
        int(parsed.port),
        True,
        parsed.username,
        parsed.password,
    )


def _read_api_credentials(json_path: str | None) -> tuple[int, str]:
    api_id = int(os.getenv("TG_API_ID", "0") or "0")
    api_hash = os.getenv("TG_API_HASH", "").strip()

    if json_path and Path(json_path).exists():
        try:
            meta = json.loads(Path(json_path).read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                raw_id = meta.get("api_id") or meta.get("app_id")
                raw_hash = meta.get("api_hash") or meta.get("app_hash")
                if raw_id:
                    api_id = int(raw_id)
                if raw_hash and isinstance(raw_hash, str):
                    api_hash = raw_hash.strip()
        except Exception:
            pass

    if not api_id or not api_hash:
        raise ValueError("Не заданы api_id / api_hash ни в .json аккаунта, ни в TG_API_ID / TG_API_HASH")

    return api_id, api_hash


def _extract_auth_from_sqlite_session(session_path: str) -> tuple[int, bytes, int | None] | None:
    """Прямое чтение dc_id, auth_key и user_id из SQLite-файла .session Telethon."""
    p = Path(session_path)
    if not p.exists():
        return None
    try:
        with sqlite3.connect(str(p)) as conn:
            row = conn.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1").fetchone()
            if not row or not row[1]:
                return None
            dc_id = int(row[0])
            auth_key = bytes(row[1])
            user_id = None
            try:
                ent = conn.execute("SELECT id FROM entities WHERE is_self = 1 LIMIT 1").fetchone()
                if ent:
                    user_id = int(ent[0])
            except sqlite3.OperationalError:
                pass
            return dc_id, auth_key, user_id
    except sqlite3.DatabaseError:
        return None


async def extract_session_auth(
    session_path: str,
    json_path: str | None = None,
    raw_proxy: str | None = None,
) -> dict:
    """Возвращает {'dc_id': int, 'auth_key_hex': str, 'user_id': int | None}."""
    direct = _extract_auth_from_sqlite_session(session_path)
    if direct and len(direct[1]) == 256:
        dc_id, auth_key, user_id = direct
        return {
            "dc_id": dc_id,
            "auth_key_hex": auth_key.hex(),
            "user_id": user_id,
        }

    api_id, api_hash = _read_api_credentials(json_path)
    proxy = _parse_telethon_proxy(raw_proxy)

    client = TelegramClient(session_path, api_id, api_hash, proxy=proxy)
    await client.connect()
    try:
        dc_id = int(client.session.dc_id)
        key_bytes = client.session.auth_key.key if client.session.auth_key else b""
        if not key_bytes or len(key_bytes) != 256:
            raise ValueError("auth_key отсутствует или некорректен в .session")
        me = await client.get_me()
        user_id = int(me.id) if me else None
        return {
            "dc_id": dc_id,
            "auth_key_hex": key_bytes.hex(),
            "user_id": user_id,
        }
    finally:
        await client.disconnect()


def build_local_storage_payload(auth_data: dict) -> dict[str, str]:
    """Формирует ключи localStorage для web.telegram.org/k/."""
    dc_id = int(auth_data["dc_id"])
    auth_key_hex = str(auth_data["auth_key_hex"])
    user_id = auth_data.get("user_id")

    # WebK использует ключи вида dc{dc_id}_auth_key, dc{dc_id}_server_salt, user_auth
    payload: dict[str, str] = {
        f"dc{dc_id}_auth_key": json.dumps(auth_key_hex),
        "dc": str(dc_id),
    }
    if user_id is not None:
        payload["user_auth"] = json.dumps(
            {
                "dcID": dc_id,
                "id": int(user_id),
            }
        )
    return payload
