"""Vessel 紐付け管理。

Stack-chan device と SAIVerse Building の紐付けを管理する。アドオン専用
SQLite (~/.saiverse/addons/saiverse-stackchan-addon/vessels.db) で永続化し、
in-memory な VesselSession で接続中の WebSocket を track する。

詳細設計: docs/intent/stackchan_vessel.md
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from saiverse.addon_paths import get_addon_storage_path

if TYPE_CHECKING:
    import asyncio
    from fastapi import WebSocket

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VesselRecord:
    """vessels テーブルの 1 行。device_token / hash は含めない。"""
    vessel_id: str
    building_id: str
    hardware_model: str
    firmware_version: Optional[str]
    paired_at: str
    last_seen_at: Optional[str]


@dataclass
class VesselSession:
    """接続中の Stack-chan device セッション。in-memory のみ、永続化しない。

    event_loop は server_hooks 等の別スレッドから WebSocket に送信する際
    (asyncio.run_coroutine_threadsafe) に必要。Phase 2 の audio_stream_bridge
    が利用する。
    """
    vessel_id: str
    building_id: str
    ws: "WebSocket"
    event_loop: Optional["asyncio.AbstractEventLoop"] = None
    connected_at: str = field(default_factory=_utcnow_iso)
    firmware_version: Optional[str] = None


class VesselManager:
    """Vessel 永続化 + アクティブセッション管理。

    永続データ (vessels.db / vessels テーブル):
      - vessel_id PRIMARY KEY
      - device_token_salt + device_token_hash (sha256 ハッシュ化)
      - building_id (紐付け Building)
      - hardware_model / firmware_version / paired_at / last_seen_at

    一時データ (in-memory):
      - _sessions: vessel_id → VesselSession
    """

    def __init__(self) -> None:
        storage = get_addon_storage_path(ADDON_NAME)
        self._db_path: Path = storage / "vessels.db"
        self._lock = threading.RLock()
        self._sessions: Dict[str, VesselSession] = {}
        self._init_db()

    # ----- DB schema management -----

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vessels (
                    vessel_id TEXT PRIMARY KEY,
                    device_token_salt TEXT NOT NULL,
                    device_token_hash TEXT NOT NULL,
                    building_id TEXT NOT NULL,
                    hardware_model TEXT NOT NULL DEFAULT 'unknown',
                    firmware_version TEXT,
                    paired_at TEXT NOT NULL,
                    last_seen_at TEXT
                )
                """
            )
            conn.commit()

    # ----- Pairing -----

    def create_pairing(
        self,
        building_id: str,
        hardware_model: str = "stackchan_ai_desktop_v1",
    ) -> tuple[str, str]:
        """新規ペアリングを発行する。

        device_token は平文で 1 回だけ返される。DB には salt + sha256 ハッシュ
        しか保存しないため、紛失時は再ペアリングが必要。

        Returns:
            (vessel_id, device_token) のタプル。
        """
        vessel_id = str(uuid.uuid4())
        device_token = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        token_hash = self._hash_token(salt, device_token)
        paired_at = _utcnow_iso()

        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO vessels (
                    vessel_id, device_token_salt, device_token_hash, building_id,
                    hardware_model, firmware_version, paired_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (vessel_id, salt, token_hash, building_id, hardware_model, paired_at),
            )
            conn.commit()

        LOGGER.info(
            "VesselManager: pairing created vessel_id=%s building_id=%s model=%s",
            vessel_id, building_id, hardware_model,
        )
        return vessel_id, device_token

    def verify_device(self, vessel_id: str, device_token: str) -> Optional[VesselRecord]:
        """device 接続時の認証。一致すれば VesselRecord を返す。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT vessel_id, device_token_salt, device_token_hash, building_id,
                       hardware_model, firmware_version, paired_at, last_seen_at
                FROM vessels WHERE vessel_id = ?
                """,
                (vessel_id,),
            ).fetchone()

        if row is None:
            LOGGER.warning(
                "VesselManager: verify_device unknown vessel_id=%s", vessel_id
            )
            return None

        v_id, salt, expected_hash, building_id, hw_model, fw_version, paired_at, last_seen = row
        actual_hash = self._hash_token(salt, device_token)

        if not hmac.compare_digest(actual_hash, expected_hash):
            LOGGER.warning(
                "VesselManager: verify_device token mismatch vessel_id=%s", vessel_id
            )
            return None

        return VesselRecord(
            vessel_id=v_id,
            building_id=building_id,
            hardware_model=hw_model,
            firmware_version=fw_version,
            paired_at=paired_at,
            last_seen_at=last_seen,
        )

    def update_firmware_version(self, vessel_id: str, firmware_version: str) -> None:
        """device 接続時に hello メッセージから firmware_version を記録する。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE vessels SET firmware_version = ? WHERE vessel_id = ?",
                (firmware_version, vessel_id),
            )
            conn.commit()

    def update_last_seen(self, vessel_id: str) -> None:
        """接続維持中の最終生存時刻を更新する。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE vessels SET last_seen_at = ? WHERE vessel_id = ?",
                (_utcnow_iso(), vessel_id),
            )
            conn.commit()

    # ----- Listing / deletion -----

    def list_vessels(self) -> List[VesselRecord]:
        """登録済み vessel 一覧。device_token / hash は含めない。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT vessel_id, building_id, hardware_model,
                       firmware_version, paired_at, last_seen_at
                FROM vessels ORDER BY paired_at ASC
                """
            ).fetchall()

        return [
            VesselRecord(
                vessel_id=r[0],
                building_id=r[1],
                hardware_model=r[2],
                firmware_version=r[3],
                paired_at=r[4],
                last_seen_at=r[5],
            )
            for r in rows
        ]

    def delete_vessel(self, vessel_id: str) -> bool:
        """ペアリング解除。

        実 WebSocket のクローズは上位呼び出し側責務 (このメソッドは
        in-memory セッションを単に外すだけ)。

        Returns:
            削除に成功したら True、未登録なら False
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM vessels WHERE vessel_id = ?", (vessel_id,)
            )
            conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            self._sessions.pop(vessel_id, None)
            LOGGER.info("VesselManager: vessel deleted vessel_id=%s", vessel_id)
        return deleted

    # ----- Session management (in-memory) -----

    def register_session(self, session: VesselSession) -> None:
        with self._lock:
            self._sessions[session.vessel_id] = session
        LOGGER.info(
            "VesselManager: session registered vessel_id=%s building_id=%s",
            session.vessel_id, session.building_id,
        )

    def unregister_session(self, vessel_id: str) -> None:
        with self._lock:
            self._sessions.pop(vessel_id, None)
        LOGGER.info("VesselManager: session unregistered vessel_id=%s", vessel_id)

    def get_session(self, vessel_id: str) -> Optional[VesselSession]:
        with self._lock:
            return self._sessions.get(vessel_id)

    def list_sessions(self) -> List[VesselSession]:
        with self._lock:
            return list(self._sessions.values())

    # ----- Helpers -----

    @staticmethod
    def _hash_token(salt: str, token: str) -> str:
        """sha256(salt || token) を hex で返す。"""
        h = hashlib.sha256()
        h.update(salt.encode("utf-8"))
        h.update(token.encode("utf-8"))
        return h.hexdigest()


# ----- Singleton accessor -----

_singleton: Optional[VesselManager] = None
_singleton_lock = threading.Lock()


def get_vessel_manager() -> VesselManager:
    """プロセス内シングルトンを返す。初回呼び出しで DB 初期化される。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = VesselManager()
    return _singleton


__all__ = [
    "VesselRecord",
    "VesselSession",
    "VesselManager",
    "get_vessel_manager",
]
