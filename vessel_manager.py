"""Vessel 紐付け管理 (v0.5: stackchan-mcp 採用版)。

Stack-chan device と SAIVerse Building / Persona の紐付けを管理する。アドオン
専用 SQLite (~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/vessels.db) で永続化。

v0.5 の認証モデル:
  - stackchan-mcp は `Authorization: Bearer <token>` のみで device を認証
  - protocol レベルには vessel_id 概念がない
  - 我々が vessels.db で `token_hash → vessel_id → bound_building_id` の対応
    テーブルを持つ
  - device は token を AP モード設定 UI で入力、addon は token から vessel_id を
    逆引きする

v0.4 までの自前ファーム時代との変更点:
  - 自前ファームは hello メッセージで `{vessel_id, device_token}` を送ってきた
    ので vessel_id を PK にできたが、stackchan-mcp は token のみ → token_hash
    も検索キーとして使えるようインデックスを張る
  - `bound_persona_id` カラムを追加 (= どのペルソナがこの vessel に紐付くか)
  - WebSocket セッション管理は gateway 側 (stackchan-mcp) に移行、addon 側の
    `VesselSession` は ws 参照を持たない軽量な state holder に縮小
  - 古いスキーマ (`device_token_salt`/`device_token_hash` のみ、bound_persona_id
    なし) からの light migration を `_init_db` 内で実施

詳細設計: docs/intent/stackchan_vessel.md (SAIVerse 本体側) v0.5 §E
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from saiverse.addon_paths import get_addon_data_dir

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"

# ペアリング時のポート割当の起点。device は NVS の固定 URL (ws://<ip>:<port>)
# に繋ぐので、ポートは機体ごとに確定して vessels.db に永続する (intent K-3)。
# 1 機体あたり ws_port / capture_port の連続ペアを 1 つ消費する。
_BASE_WS_PORT = 8765

# list/get 系が共有する SELECT 列順 (= ``_row_to_record`` の row index と
# 1:1 対応)。token salt/hash は含めない。
_RECORD_COLUMNS = (
    "vessel_id, bound_building_id, bound_persona_id, hardware_model, "
    "firmware_version, paired_at, last_seen_at, ws_port, capture_port, "
    "capabilities, unit_config"
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VesselRecord:
    """vessels テーブルの 1 行。token は含めない。

    平文 token はペアリング時に 1 回だけ ``create_pairing`` の返り値で渡され、
    その後は DB に残らない (= salt + sha256 ハッシュのみ保存)。
    """
    vessel_id: str
    bound_building_id: str
    bound_persona_id: Optional[str]
    hardware_model: str
    firmware_version: Optional[str]
    paired_at: str
    last_seen_at: Optional[str]
    ws_port: Optional[int] = None
    capture_port: Optional[int] = None
    capabilities: Optional[Dict[str, Any]] = None
    # per-vessel「ハブ + チャンネル配置」({version, hub, units})。NULL の間は
    # capabilities にフォールバックする (vessel_dispatch.effective_units 参照)。
    unit_config: Optional[Dict[str, Any]] = None


@dataclass
class VesselSession:
    """接続中の Stack-chan device セッションの軽量 state holder。

    v0.5 では WebSocket 自体は stackchan-mcp gateway (subprocess) が管理する
    ので、addon 側はその接続情報を直接保持しない。代わりに「いつ最後に発話を
    流したか」「ペアリングされた vessel/persona/building は何か」程度を覚えて
    おく。speak_hook が「Vessel Building に居るペルソナか」を判定する用途。
    """
    vessel_id: str
    bound_building_id: str
    bound_persona_id: Optional[str]
    firmware_version: Optional[str] = None
    connected_at: str = field(default_factory=_utcnow_iso)
    last_activity_at: str = field(default_factory=_utcnow_iso)


class VesselManager:
    """Vessel 永続化 + アクティブセッション state 管理 (v0.5)。

    Phase 1' は **single vessel 前提**。`create_pairing` を 2 回以上呼ぶと
    複数 row が並ぶが、stackchan-mcp gateway が認識する master_token は 1 個
    のみ (= 環境変数 ``STACKCHAN_TOKEN``)。複数 vessel 対応は Phase 2' 以降で
    upstream PR (= gateway の multi-token validation) と組み合わせて実装する。
    """

    def __init__(self) -> None:
        storage = get_addon_data_dir(ADDON_NAME)
        self._db_path: Path = storage / "vessels.db"
        self._lock = threading.RLock()
        self._sessions: Dict[str, VesselSession] = {}
        self._init_db()

    # ----- DB schema management -----

    def _init_db(self) -> None:
        """v0.5 スキーマで初期化、既存 v0.4 スキーマからは light migration。

        Migration 内容:
          - ``bound_persona_id`` カラム追加 (NULL 許容、デフォルト NULL)
          - ``building_id`` → ``bound_building_id`` のリネーム (= v0.5 表現に
            揃える、ただし CREATE TABLE で新規時は新名で作る)。既存 column
            ``building_id`` が残っているケースは、`ALTER TABLE RENAME COLUMN`
            (SQLite 3.25+) で対応
          - ``device_token_hash`` のインデックス追加 (= token から逆引き)
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vessels (
                    vessel_id TEXT PRIMARY KEY,
                    device_token_salt TEXT NOT NULL,
                    device_token_hash TEXT NOT NULL,
                    bound_building_id TEXT NOT NULL,
                    bound_persona_id TEXT,
                    hardware_model TEXT NOT NULL DEFAULT 'unknown',
                    firmware_version TEXT,
                    paired_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    ws_port INTEGER,
                    capture_port INTEGER,
                    capabilities TEXT
                )
                """
            )
            # Light migration: 既存 v0.4 スキーマからのアップグレード
            cols = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(vessels)").fetchall()
            }
            if "bound_persona_id" not in cols:
                conn.execute(
                    "ALTER TABLE vessels ADD COLUMN bound_persona_id TEXT"
                )
                LOGGER.info(
                    "VesselManager: migrated schema - added bound_persona_id column"
                )
            if "building_id" in cols and "bound_building_id" not in cols:
                # SQLite 3.25+ で RENAME COLUMN サポート、Python 3.11 同梱の
                # sqlite3 は 3.40+ なので問題なく動く
                conn.execute(
                    "ALTER TABLE vessels RENAME COLUMN building_id TO bound_building_id"
                )
                LOGGER.info(
                    "VesselManager: migrated schema - renamed building_id to "
                    "bound_building_id"
                )
            # v0.10 マルチ機体: per-vessel のポート (ペアリング時確定・永続) と
            # capability (搭載ユニット集合) カラムを追加 (intent K-3 / K-5)。
            # unit_config (v0.11): per-vessel の「ハブ + チャンネル配置」JSON
            # ({version, hub, units})。従来の capabilities (bool 辞書) を包含する
            # 上位モデル。NULL の間は capabilities + グローバル hub にフォール
            # バックする (additive・非破壊)。設計: docs/intent/stackchan_unit_placement.md
            for col, ddl in (
                ("ws_port", "ALTER TABLE vessels ADD COLUMN ws_port INTEGER"),
                ("capture_port",
                 "ALTER TABLE vessels ADD COLUMN capture_port INTEGER"),
                ("capabilities",
                 "ALTER TABLE vessels ADD COLUMN capabilities TEXT"),
                ("unit_config",
                 "ALTER TABLE vessels ADD COLUMN unit_config TEXT"),
            ):
                if col not in cols:
                    conn.execute(ddl)
                    LOGGER.info(
                        "VesselManager: migrated schema - added %s column", col
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vessels_token_hash "
                "ON vessels(device_token_hash)"
            )
            conn.commit()

    # ----- Pairing -----

    def create_pairing(
        self,
        building_id: str,
        persona_id: Optional[str] = None,
        hardware_model: str = "stackchan_kickstarter_2025",
        device_token: Optional[str] = None,
    ) -> tuple[str, str]:
        """新規ペアリングを発行する。

        device_token は平文で 1 回だけ返される。DB には salt + sha256 ハッシュ
        しか保存しないため、紛失時は再ペアリングが必要。

        呼び出し側で:
          - 返り値の ``device_token`` を addon の AddonConfig (``master_token``
            キー) に保存する → ``mcp_servers.json`` の env で stackchan-mcp
            gateway に渡る
          - ユーザーには QR コード or 手入力フォームで token を提示し、device
            の AP モード設定 UI に同じ値を入力させる (Phase 2' UX)

        Args:
            building_id: Vessel Building の ID (= ペルソナが「物理身体に降りる」
                ための Building、capacity=1)
            persona_id: バインドするペルソナの ID。ペアリング時に既に決まって
                いれば指定、未定なら ``None`` で OK (後で ``bind_persona``)
            hardware_model: 機種識別子。Phase 1' は固定値 (Kickstarter 版 CoreS3)

        Returns:
            ``(vessel_id, device_token)`` のタプル。
        """
        vessel_id = str(uuid.uuid4())
        # device_token 未指定なら新規生成 (1 台目)。複数機体では呼び出し側が
        # 既存の共通 master_token を渡し、 全機体で同一トークンを使う
        # (token 共通・機体区別はポート、 intent K-7)。
        if device_token is None:
            device_token = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        token_hash = self._hash_token(salt, device_token)
        paired_at = _utcnow_iso()

        with self._lock, sqlite3.connect(self._db_path) as conn:
            ws_port, capture_port = self._allocate_ports(conn)
            conn.execute(
                """
                INSERT INTO vessels (
                    vessel_id, device_token_salt, device_token_hash,
                    bound_building_id, bound_persona_id, hardware_model,
                    firmware_version, paired_at, last_seen_at,
                    ws_port, capture_port, capabilities
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    vessel_id, salt, token_hash, building_id, persona_id,
                    hardware_model, paired_at,
                    ws_port, capture_port, json.dumps({}),
                ),
            )
            conn.commit()

        LOGGER.info(
            "VesselManager: pairing created vessel_id=%s building_id=%s "
            "persona_id=%s model=%s ws_port=%d capture_port=%d",
            vessel_id, building_id, persona_id, hardware_model,
            ws_port, capture_port,
        )
        return vessel_id, device_token

    def verify_token(self, token: str) -> Optional[VesselRecord]:
        """token 認証 (v0.5、device の Authorization: Bearer 用)。

        protocol レベルに vessel_id がない (stackchan-mcp の Bearer 認証モデル)
        ため、token から vessel_id を逆引きする。Phase 1' は single vessel 前提
        だが、複数 row があっても順次比較で対応 (= multi-token は Phase 2'+
        以降で upstream PR と組み合わせる)。

        Returns:
            一致する VesselRecord、なければ None。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT device_token_salt, device_token_hash, "
                f"{_RECORD_COLUMNS} FROM vessels"
            ).fetchall()

        for row in rows:
            salt = row[0]
            expected_hash = row[1]
            actual_hash = self._hash_token(salt, token)
            if hmac.compare_digest(actual_hash, expected_hash):
                # 先頭の salt/hash を除けば _RECORD_COLUMNS 順の tuple
                return self._row_to_record(tuple(row[2:]))

        LOGGER.debug("VesselManager: verify_token no match")
        return None

    def bind_persona(self, vessel_id: str, persona_id: Optional[str]) -> bool:
        """vessel に紐付くペルソナを更新する (None で紐付け解除)。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE vessels SET bound_persona_id = ? WHERE vessel_id = ?",
                (persona_id, vessel_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_capabilities(
        self, vessel_id: str, capabilities: Dict[str, Any]
    ) -> bool:
        """機体の capability (搭載ユニット集合・ハブ構成等) を保存する。

        手動設定が基盤 (機体管理 UI から)。 自動検出 (Phase 8') もここに
        書き込む。 ペルソナがその機体に降りているとき、 ユニット由来ツールの
        可視性をこの値から決める (intent K-5、 不変条件 #14)。
        """
        payload = json.dumps(capabilities, ensure_ascii=False)
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE vessels SET capabilities = ? WHERE vessel_id = ?",
                (payload, vessel_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_unit_config(
        self, vessel_id: str, unit_config: Dict[str, Any]
    ) -> bool:
        """機体のユニット配置 ({version, hub, units}) を保存する。

        機体管理 UI の配置エディタから設定する上位モデル。従来の
        ``set_capabilities`` (bool 辞書) を包含する。ユニット可視性・i2c の
        チャンネル解決の source になる (docs/intent/stackchan_unit_placement.md)。
        NULL のうちは ``capabilities`` にフォールバックする
        (``vessel_dispatch.effective_units``)。
        """
        payload = json.dumps(unit_config, ensure_ascii=False)
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE vessels SET unit_config = ? WHERE vessel_id = ?",
                (payload, vessel_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_ports(
        self, vessel_id: str, ws_port: int, capture_port: int
    ) -> bool:
        """ws_port / capture_port を明示的に設定する (再ペアリング・手動変更用)。

        通常はペアリング時に ``create_pairing`` が自動割当するので呼ぶ必要は
        ない。 device 側 NVS の URL と一致させる責務は呼び出し側にある。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE vessels SET ws_port = ?, capture_port = ? "
                "WHERE vessel_id = ?",
                (ws_port, capture_port, vessel_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_firmware_version(
        self, vessel_id: str, firmware_version: str
    ) -> None:
        """device から取得した firmware_version を記録する。"""
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
        """登録済み vessel 一覧。token / hash は含めない。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM vessels ORDER BY paired_at ASC"
            ).fetchall()

        return [self._row_to_record(r) for r in rows]

    def get_vessel(self, vessel_id: str) -> Optional[VesselRecord]:
        """単一 vessel の取得。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM vessels WHERE vessel_id = ?",
                (vessel_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_vessel_by_building(self, building_id: str) -> Optional[VesselRecord]:
        """building_id に紐付く vessel を返す (Vessel Building は capacity=1 なので
        1 機体)。 入退室フック (vessel_gateways) や avatar_loader が、 引数の
        building_id から機体を引くのに使う (= persona context に依らない)。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM vessels "
                "WHERE bound_building_id = ? ORDER BY paired_at ASC LIMIT 1",
                (building_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_vessel_for_persona(
        self, persona_id: str, building_id: str
    ) -> Optional[VesselRecord]:
        """ペルソナ + Building の組み合わせから vessel を逆引きする。

        二段階で探す:

        1. **persona 専用バインド** (`bound_persona_id = persona_id AND
           bound_building_id = building_id`): この vessel は特定 persona に
           bind されている、それ以外の persona は使えない
        2. **persona 未指定バインド** (`bound_persona_id IS NULL AND
           bound_building_id = building_id`): Vessel Building 内なら誰でも
           使える状態 (Vessel Building は capacity=1 なので結果として
           「Building にいる persona が使う」と同じ意味になる)

        speak_hook が「このペルソナがこの Building で物理身体に降りているか」
        を判定する用途。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            # 1. persona 専用バインドを優先
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM vessels "
                "WHERE bound_persona_id = ? AND bound_building_id = ? LIMIT 1",
                (persona_id, building_id),
            ).fetchone()
            # 2. persona 未指定 (NULL) でフォールバック
            if row is None:
                row = conn.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM vessels "
                    "WHERE bound_persona_id IS NULL AND bound_building_id = ? "
                    "LIMIT 1",
                    (building_id,),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def delete_vessel(self, vessel_id: str) -> bool:
        """ペアリング解除。

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
            with self._lock:
                self._sessions.pop(vessel_id, None)
            LOGGER.info("VesselManager: vessel deleted vessel_id=%s", vessel_id)
        return deleted

    # ----- Session state (in-memory, no WebSocket reference) -----

    def register_session(self, session: VesselSession) -> Optional[VesselSession]:
        """新しい session state を登録する。同じ vessel_id の既存があれば
        上書きして古いものを返す。

        v0.4 までは WebSocket 参照を持っていたが、v0.5 では state holder
        のみ (gateway が WS を管理する) なので「古いを close する」処理は
        呼び出し側で不要。
        """
        with self._lock:
            old = self._sessions.get(session.vessel_id)
            self._sessions[session.vessel_id] = session
        if old is not None and old is not session:
            LOGGER.warning(
                "VesselManager: session state replaced vessel_id=%s",
                session.vessel_id,
            )
        LOGGER.info(
            "VesselManager: session registered vessel_id=%s building_id=%s "
            "persona_id=%s",
            session.vessel_id, session.bound_building_id,
            session.bound_persona_id,
        )
        return old if (old is not None and old is not session) else None

    def unregister_session(self, vessel_id: str) -> None:
        """session state を登録解除する。"""
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
    def _parse_json_dict(raw: Any, field: str, vessel_id: str) -> Optional[Dict[str, Any]]:
        """TEXT カラムの JSON dict を parse する。壊れていれば None + WARNING。"""
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "VesselManager: %s JSON 解釈失敗 vessel_id=%s", field, vessel_id
            )
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _row_to_record(row) -> VesselRecord:
        """``_RECORD_COLUMNS`` 順の row tuple を VesselRecord に変換する。"""
        caps = VesselManager._parse_json_dict(row[9], "capabilities", row[0])
        unit_cfg = VesselManager._parse_json_dict(row[10], "unit_config", row[0])
        return VesselRecord(
            vessel_id=row[0],
            bound_building_id=row[1],
            bound_persona_id=row[2],
            hardware_model=row[3],
            firmware_version=row[4],
            paired_at=row[5],
            last_seen_at=row[6],
            ws_port=row[7],
            capture_port=row[8],
            capabilities=caps,
            unit_config=unit_cfg,
        )

    def _allocate_ports(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """既存 vessel と衝突しない ws_port / capture_port の連続ペアを返す。

        ``_BASE_WS_PORT`` から 2 ずつ進め、 vessels.db 内で未使用の連続ペアを
        探す。 OS レベルの空き確認はここではしない (= 起動時に gateway が
        bind 失敗したら別途対処)。 呼び出し側のトランザクション (conn) 内で
        使う前提。 device は NVS の固定 URL に繋ぐため、 一度確定した値は
        その機体に永続する (intent K-3)。
        """
        used: set[int] = set()
        for ws, cap in conn.execute(
            "SELECT ws_port, capture_port FROM vessels "
            "WHERE ws_port IS NOT NULL OR capture_port IS NOT NULL"
        ).fetchall():
            if ws is not None:
                used.add(int(ws))
            if cap is not None:
                used.add(int(cap))
        ws_port = _BASE_WS_PORT
        while ws_port in used or (ws_port + 1) in used:
            ws_port += 2
        return ws_port, ws_port + 1

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
