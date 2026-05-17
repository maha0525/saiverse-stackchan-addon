"""Stack-chan Vessel: avatar セット生成パイプライン (Phase 4.5-d)。

ペルソナごとに複数の avatar セット (= 衣装違い / 髪型違い / 表情パレット違い)
を WIP として保持し、 段階順 (① 元顔 → ② 表情差分 5 種 → ③ 目・口差分 →
④ 一括トリミング → ⑤ リサイズ + RGB565 変換 → ⑥ Stack-chan 転送) で構築
していく。 確定品 (= avatar.bin + manifest.json) は既存 avatar_loader.py
のスキーマと整合する。

ストレージレイアウト:

    ~/.saiverse/addons/saiverse-stackchan-addon/avatar_sets/
        <persona_id>/
            _active.json         # アクティブセット名 (D-8)
            <set_name>/
                manifest.json    # 確定品 (load_avatar_set 用、 既存 schema)
                avatar.bin       # 確定品 (RGB565 raw 連結、 既存 schema)
                wip/
                    metadata.json     # 進捗 + 共通プロンプト + 各段階の追加自由文
                    01_face/face.png
                    02_expressions/{happy,thinking,sad,surprised,embarrassed}.png
                    03_matrix/{face}_{eyes}_{mouth}.png      (matrix mode)
                        または 03_layered/{eyes_<s>, mouth_<m>}.png (layered)
                    04_trimmed/{③ と同構造}

設計詳細: docs/intent/stackchan_avatar_pipeline.md §D-3〜D-8

Phase 4.5-d-1 (本ファイル): WIP storage schema + state management のみ実装。
段階別実行 (= 実際の image_generator 呼び出し) と単発再生成は Phase 4.5-d-2
で `register_stage_executor` 経由で hook 注入する形にする。
"""
import json
import logging
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# addon_loader の spec_from_file_location 経由ロードでは __package__ が
# 設定されないため相対 import が動かない。 同梱モジュールを絶対 import
# するためにパック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from saiverse.addon_paths import get_addon_storage_path  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"

# 段階 ID (= ディレクトリ名と一致)。 値はディスク上のサブディレクトリ名。
STAGE_FACE = "01_face"
STAGE_EXPRESSIONS = "02_expressions"
STAGE_MATRIX = "03_matrix"      # mode=matrix の時の ③
STAGE_LAYERED = "03_layered"    # mode=layered の時の ③
STAGE_TRIMMED = "04_trimmed"

# 段階順序 (= UI の段階バーと対応)。
STAGE_ORDER_MATRIX = [STAGE_FACE, STAGE_EXPRESSIONS, STAGE_MATRIX, STAGE_TRIMMED]
STAGE_ORDER_LAYERED = [STAGE_FACE, STAGE_EXPRESSIONS, STAGE_LAYERED, STAGE_TRIMMED]

# 表情ラベル (Phase 4.5-a の AvatarSet face_index と対応)。
FACE_NAMES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
# ②の追加生成対象 (= ①の idle 以外の 5 種)。
EXPRESSION_NAMES = ["happy", "thinking", "sad", "surprised", "embarrassed"]
# 目状態 (Phase 4.5-a の AvatarSet eyes_index と対応)。
EYES_STATES = ["open", "half", "closed"]
# 口状態 (Phase 4.5-a の AvatarSet mouth_index と対応)。
MOUTH_SHAPES = ["closed", "half", "open", "e", "u"]

# matrix mode の総枚数 (= 6 表情 × 目 3 × 口 5)。
MATRIX_TOTAL_FRAMES = len(FACE_NAMES) * len(EYES_STATES) * len(MOUTH_SHAPES)

VALID_MODES = ("layered", "matrix")
METADATA_VERSION = 1


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StageOutputInfo:
    """段階出力 1 ファイルの情報 (= プレビュー用)。"""
    target: str           # 例: "face", "happy", "happy_open_closed"
    path: str             # 絶対パス
    size_bytes: int


@dataclass
class StageState:
    """1 段階の状態。"""
    stage_id: str
    files: list[StageOutputInfo] = field(default_factory=list)
    completed: bool = False  # ユーザーが「次へ」 を押した = 後段に進めて OK


@dataclass
class AvatarSetMetadata:
    """`wip/metadata.json` の内容を表現する dataclass。

    確定品 (= `manifest.json`) とは別物。 WIP の進行状態 + 編集中プロンプトを
    持つ作業用 metadata。
    """
    version: int = METADATA_VERSION
    mode: str = "matrix"
    common_prompt: str = ""
    # 段階別の追加自由文。 stage_id -> target -> extra text。
    # 例: extra_prompts["02_expressions"]["happy"] = "smiling brightly"
    extra_prompts: dict[str, dict[str, str]] = field(default_factory=dict)
    # ④ 一括トリミングの矩形 (元画像サイズに対する絶対座標)。
    trim_rect: Optional[dict[str, int]] = None
    # ④ per-target トリミング矩形 (target name → rect dict)。
    # まはー指摘: 表情ごとに顔の中心位置が変わるので、 一律 trim_rect だと
    # 揃わない。 ここに登録された target は default trim_rect ではなく per-target
    # rect が使われる。 未登録なら default trim_rect が適用。
    trim_rect_overrides: dict[str, dict[str, int]] = field(default_factory=dict)
    # 並列度 (= ③ の生成で同時に投げる API リクエスト数)。
    parallelism: int = 5
    # 生成 backend モデル (= image_generator の ModelType)。
    # default = gpt_image_2: まはー検証で「low でも余裕で綺麗、 5 枚 $0.10
    # で安価、 SAIVerse ユーザー層は OpenAI API key 持ってる前提」 で確定
    # (2026-05-17)。 nano_banana 系へ切替は Debug フラグから可能。
    image_model: str = "gpt_image_2"
    # 生成解像度 (image_generator の QualityType: "low" / "medium" / "high" / "auto")。
    # default = low: gpt_image_2 で low でも目パチ口パク用途で十分な品質
    # (= まはー検証)。 nano_banana 系に切替えた場合は high 推奨。
    image_quality: str = "low"
    # アスペクト比 (image_generator の AspectRatioType: "1:1" / "16:9" / "4:3" 等)。
    # ① で外部画像をアップロード経路で配置した時、 その crop アス比が
    # ここに保存される (= ②③ も同アス比で生成、 目パチ口パクの座標ズレ防止)。
    aspect_ratio: str = "1:1"
    # 段階別の quality 上書き (= 例: {"02_expressions": "medium"})。
    # Debug フラグ ON 時に UI から編集可能、 OFF 時は image_quality 一律。
    stage_quality_overrides: dict[str, str] = field(default_factory=dict)
    # 段階別の aspect_ratio 上書き。 通常は使わない (= セット内で揃えるべき) が、
    # Debug 時の検証用に提供。
    stage_aspect_overrides: dict[str, str] = field(default_factory=dict)
    # ③ で共通プロンプトを使うか (= default False、 まはー検証 2026-05-17)。
    # ③は目・口の差分生成だけが目的で、 共通プロンプトに外見情報 (アクセサリ
    # 等) があると差分として勝手に追加されてしまう (= 元画像にないものが出る)。
    # default で off、 ユーザーが Debug から on にできる。
    apply_common_prompt_to_stage3: bool = False
    # 各段階の完了フラグ (= ユーザーが「次へ」 を押した段階)。
    completed_stages: list[str] = field(default_factory=list)
    # 現在の段階 (= UI で開いている段階)。
    current_stage: str = STAGE_FACE
    # WIP 作成日時 / 最終更新日時 (ISO 8601 UTC)。
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AvatarSetMetadata":
        # 未知のキーは無視 (= forward compat)、 欠けてるキーは default で埋まる。
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class SetInfo:
    """単一セットの状態スナップショット (= API レスポンス用)。"""
    set_name: str
    persona_id: str
    has_finalized: bool          # avatar.bin + manifest.json が揃っている
    finalized_mode: Optional[str]
    finalized_checksum: Optional[str]
    has_wip: bool                # wip/metadata.json がある
    wip_metadata: Optional[AvatarSetMetadata]
    wip_stages: list[StageState] = field(default_factory=list)
    is_active: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "set_name": self.set_name,
            "persona_id": self.persona_id,
            "has_finalized": self.has_finalized,
            "finalized_mode": self.finalized_mode,
            "finalized_checksum": self.finalized_checksum,
            "has_wip": self.has_wip,
            "wip_metadata": self.wip_metadata.to_json() if self.wip_metadata else None,
            "wip_stages": [
                {
                    "stage_id": s.stage_id,
                    "completed": s.completed,
                    "files": [asdict(f) for f in s.files],
                }
                for s in self.wip_stages
            ],
            "is_active": self.is_active,
        }


# ----- Stage executor hook (= Phase 4.5-d-2 で hook 注入) -----

StageExecutor = Callable[
    ["AvatarPipelineManager", str, str, str, dict[str, Any]], dict[str, Any]
]
"""段階別実行の hook 型。

引数: (manager, persona_id, set_name, stage_id, params)
戻り値: 任意の dict (= 実行結果、 ファイルパス / エラー情報など)
"""

RegenerateExecutor = Callable[
    ["AvatarPipelineManager", str, str, str, str, dict[str, Any]], dict[str, Any]
]
"""単発再生成の hook 型。

引数: (manager, persona_id, set_name, stage_id, target, params)
戻り値: 任意の dict
"""


class AvatarPipelineManager:
    """avatar セット WIP の永続化 + state 管理 (singleton)。

    Phase 4.5-d-1 では storage 層と state machine のみ。 実際の画像生成
    (= `execute_stage` / `regenerate_target`) は外部から hook で注入される
    (= Phase 4.5-d-2)。 hook が未登録の場合は NotImplementedError。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stage_executor: Optional[StageExecutor] = None
        self._regenerate_executor: Optional[RegenerateExecutor] = None
        # 同 persona+set の execute_stage 並列実行を防ぐ (= chain 中の 502
        # 後に frontend が next request を投げると、 前 request の処理中に
        # backend で並列実行 → OpenAI cost 倍増 + base copy 競合の事故防止、
        # まはー検証 2026-05-17)。 lock を取得できなければ wait。
        self._set_exec_locks: dict[tuple[str, str], threading.Lock] = {}
        self._set_exec_locks_guard = threading.Lock()

    # ----- Storage layout -----

    def storage_root(self) -> Path:
        return get_addon_storage_path(ADDON_NAME) / "avatar_sets"

    def persona_dir(self, persona_id: str) -> Path:
        _validate_id_component(persona_id, "persona_id")
        return self.storage_root() / persona_id

    def set_dir(self, persona_id: str, set_name: str) -> Path:
        _validate_id_component(set_name, "set_name")
        return self.persona_dir(persona_id) / set_name

    def wip_dir(self, persona_id: str, set_name: str) -> Path:
        return self.set_dir(persona_id, set_name) / "wip"

    def stage_dir(
        self, persona_id: str, set_name: str, stage_id: str
    ) -> Path:
        _validate_stage_id(stage_id)
        return self.wip_dir(persona_id, set_name) / stage_id

    def metadata_path(self, persona_id: str, set_name: str) -> Path:
        return self.wip_dir(persona_id, set_name) / "metadata.json"

    def finalized_bin_path(self, persona_id: str, set_name: str) -> Path:
        return self.set_dir(persona_id, set_name) / "avatar.bin"

    def finalized_manifest_path(
        self, persona_id: str, set_name: str
    ) -> Path:
        return self.set_dir(persona_id, set_name) / "manifest.json"

    def active_path(self, persona_id: str) -> Path:
        return self.persona_dir(persona_id) / "_active.json"

    # ----- Set CRUD -----

    def list_sets(self, persona_id: str) -> list[SetInfo]:
        """ペルソナの全セット (= バリエーション) 一覧。"""
        pdir = self.persona_dir(persona_id)
        if not pdir.exists():
            return []
        active_name = self.get_active(persona_id)
        results: list[SetInfo] = []
        for entry in sorted(pdir.iterdir()):
            if not entry.is_dir():
                continue
            info = self._build_set_info(persona_id, entry.name, active_name)
            results.append(info)
        return results

    def create_set(
        self,
        persona_id: str,
        set_name: str,
        mode: str = "matrix",
        common_prompt: str = "",
        image_model: str = "gpt_image_2",
    ) -> SetInfo:
        """新規セットを作成 (= WIP のみ、 確定品はまだ無い)。

        既存セット名と衝突したら ValueError。
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode: {mode!r} (allowed: {VALID_MODES})"
            )
        with self._lock:
            sdir = self.set_dir(persona_id, set_name)
            if sdir.exists():
                raise ValueError(
                    f"Set already exists: persona={persona_id} name={set_name}"
                )
            wdir = self.wip_dir(persona_id, set_name)
            wdir.mkdir(parents=True, exist_ok=False)
            meta = AvatarSetMetadata(
                mode=mode,
                common_prompt=common_prompt,
                image_model=image_model,
            )
            self._write_metadata(persona_id, set_name, meta)
            LOGGER.info(
                "avatar_pipeline: created set persona=%s name=%s mode=%s",
                persona_id, set_name, mode,
            )
            return self._build_set_info(
                persona_id, set_name, self.get_active(persona_id)
            )

    def get_set(self, persona_id: str, set_name: str) -> Optional[SetInfo]:
        sdir = self.set_dir(persona_id, set_name)
        if not sdir.exists():
            return None
        return self._build_set_info(
            persona_id, set_name, self.get_active(persona_id)
        )

    def delete_set(
        self,
        persona_id: str,
        set_name: str,
        wip_only: bool = False,
    ) -> bool:
        """セット削除。 `wip_only=True` なら `wip/` のみ削除して確定品は残す。

        Returns True if something was deleted, False if nothing existed.
        """
        with self._lock:
            sdir = self.set_dir(persona_id, set_name)
            if not sdir.exists():
                return False
            if wip_only:
                wdir = self.wip_dir(persona_id, set_name)
                if not wdir.exists():
                    return False
                shutil.rmtree(wdir)
                LOGGER.info(
                    "avatar_pipeline: deleted WIP only persona=%s name=%s",
                    persona_id, set_name,
                )
                return True
            shutil.rmtree(sdir)
            LOGGER.info(
                "avatar_pipeline: deleted whole set persona=%s name=%s",
                persona_id, set_name,
            )
            # アクティブセットだったらクリア。
            if self.get_active(persona_id) == set_name:
                self.set_active(persona_id, None)
            return True

    # ----- Metadata I/O -----

    def read_metadata(
        self, persona_id: str, set_name: str
    ) -> Optional[AvatarSetMetadata]:
        mpath = self.metadata_path(persona_id, set_name)
        if not mpath.exists():
            return None
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "avatar_pipeline: failed to parse metadata %s: %s",
                mpath, exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        return AvatarSetMetadata.from_json(data)

    def update_metadata(
        self,
        persona_id: str,
        set_name: str,
        **updates: Any,
    ) -> AvatarSetMetadata:
        """metadata.json の特定フィールドを更新。"""
        with self._lock:
            meta = self.read_metadata(persona_id, set_name)
            if meta is None:
                raise FileNotFoundError(
                    f"WIP metadata not found: persona={persona_id} name={set_name}"
                )
            for key, value in updates.items():
                if not hasattr(meta, key):
                    raise ValueError(f"Unknown metadata field: {key!r}")
                setattr(meta, key, value)
            meta.updated_at = _utcnow_iso()
            self._write_metadata(persona_id, set_name, meta)
            return meta

    def touch_updated_at(
        self, persona_id: str, set_name: str,
    ) -> None:
        """metadata.updated_at だけを現在時刻に更新する。

        生成系の関数 (= ファイル書き換え) は updated_at を必ず touch する
        必要がある (= frontend が `?t=${updated_at}` で cache buster
        してるので、 touch しないとサムネが古い画像のまま browser cache される)。
        """
        with self._lock:
            meta = self.read_metadata(persona_id, set_name)
            if meta is None:
                return  # WIP 未作成なら no-op (= 例: ⑥ transfer 後など)
            meta.updated_at = _utcnow_iso()
            self._write_metadata(persona_id, set_name, meta)

    def mark_stage_completed(
        self, persona_id: str, set_name: str, stage_id: str
    ) -> AvatarSetMetadata:
        """段階完了フラグを立てる (= ユーザーが「次へ」 を押した)。"""
        _validate_stage_id(stage_id)
        with self._lock:
            meta = self.read_metadata(persona_id, set_name)
            if meta is None:
                raise FileNotFoundError(
                    f"WIP metadata not found: persona={persona_id} name={set_name}"
                )
            if stage_id not in meta.completed_stages:
                meta.completed_stages.append(stage_id)
            meta.updated_at = _utcnow_iso()
            self._write_metadata(persona_id, set_name, meta)
            return meta

    def _write_metadata(
        self, persona_id: str, set_name: str, meta: AvatarSetMetadata
    ) -> None:
        mpath = self.metadata_path(persona_id, set_name)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(
            json.dumps(meta.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ----- Active set management (D-8) -----

    def get_active(self, persona_id: str) -> Optional[str]:
        """ペルソナのアクティブセット名を返す。"""
        apath = self.active_path(persona_id)
        if not apath.exists():
            return None
        try:
            data = json.loads(apath.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "avatar_pipeline: failed to read _active.json %s: %s",
                apath, exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("set_name")
        return name if isinstance(name, str) else None

    def set_active(
        self, persona_id: str, set_name: Optional[str]
    ) -> None:
        """アクティブセット名を設定。 `set_name=None` でクリア。"""
        with self._lock:
            pdir = self.persona_dir(persona_id)
            pdir.mkdir(parents=True, exist_ok=True)
            apath = self.active_path(persona_id)
            if set_name is None:
                if apath.exists():
                    apath.unlink()
                LOGGER.info(
                    "avatar_pipeline: cleared active set for persona=%s",
                    persona_id,
                )
                return
            # 存在チェック (= 存在しないセットをアクティブにすると後で
            # avatar_loader が困る)。
            sdir = self.set_dir(persona_id, set_name)
            if not sdir.exists():
                raise FileNotFoundError(
                    f"Cannot activate non-existent set: persona={persona_id} "
                    f"name={set_name}"
                )
            apath.write_text(
                json.dumps({"set_name": set_name}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            LOGGER.info(
                "avatar_pipeline: set active for persona=%s name=%s",
                persona_id, set_name,
            )

    # ----- Stage execution (hook 経由) -----

    def register_stage_executor(self, executor: StageExecutor) -> None:
        """Phase 4.5-d-2 で画像生成 hook を注入する。"""
        self._stage_executor = executor

    def register_regenerate_executor(
        self, executor: RegenerateExecutor
    ) -> None:
        self._regenerate_executor = executor

    def _get_exec_lock(
        self, persona_id: str, set_name: str,
    ) -> threading.Lock:
        key = (persona_id, set_name)
        with self._set_exec_locks_guard:
            lock = self._set_exec_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._set_exec_locks[key] = lock
            return lock

    def execute_stage(
        self,
        persona_id: str,
        set_name: str,
        stage_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """段階実行を hook に委譲。 Phase 4.5-d-2 未実装時は NotImplemented。

        実行完了後に metadata.updated_at を touch (= frontend cache buster
        `?t=${updated_at}` の invalidate、 サムネ古表示問題の防止)。
        """
        _validate_stage_id(stage_id)
        if self._stage_executor is None:
            raise NotImplementedError(
                "Stage executor not registered yet (Phase 4.5-d-2). "
                f"Requested: persona={persona_id} set={set_name} stage={stage_id}"
            )
        # per persona+set lock (= chain 中の並列実行防止)。 lock を待つ間
        # frontend の HTTP request は block するが、 前の request が完了
        # すれば順次処理される (= proxy timeout で繰り返し投げられた request
        # を queue で順次処理、 backend cost 倍増の事故防止)。
        with self._get_exec_lock(persona_id, set_name):
            try:
                return self._stage_executor(
                    self, persona_id, set_name, stage_id, params or {}
                )
            finally:
                self.touch_updated_at(persona_id, set_name)

    def regenerate_target(
        self,
        persona_id: str,
        set_name: str,
        stage_id: str,
        target: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """単発再生成を hook に委譲。 完了後に updated_at touch (= cache buster)。"""
        _validate_stage_id(stage_id)
        if self._regenerate_executor is None:
            raise NotImplementedError(
                "Regenerate executor not registered yet (Phase 4.5-d-2). "
                f"Requested: persona={persona_id} set={set_name} "
                f"stage={stage_id} target={target}"
            )
        try:
            return self._regenerate_executor(
                self, persona_id, set_name, stage_id, target, params or {}
            )
        finally:
            self.touch_updated_at(persona_id, set_name)

    # ----- Internal helpers -----

    def _build_set_info(
        self,
        persona_id: str,
        set_name: str,
        active_name: Optional[str],
    ) -> SetInfo:
        # 確定品の状態。
        bin_path = self.finalized_bin_path(persona_id, set_name)
        manifest_path = self.finalized_manifest_path(persona_id, set_name)
        has_finalized = bin_path.exists() and manifest_path.exists()
        finalized_mode: Optional[str] = None
        finalized_checksum: Optional[str] = None
        if has_finalized:
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                if isinstance(manifest, dict):
                    finalized_mode = manifest.get("mode")
                    finalized_checksum = manifest.get("checksum")
            except Exception as exc:
                LOGGER.warning(
                    "avatar_pipeline: failed to read manifest %s: %s",
                    manifest_path, exc,
                )
        # WIP の状態。
        meta = self.read_metadata(persona_id, set_name)
        has_wip = meta is not None
        stages: list[StageState] = []
        if meta is not None:
            for stage_id in _stage_order_for_mode(meta.mode):
                stage = self._build_stage_state(
                    persona_id, set_name, stage_id, meta,
                )
                stages.append(stage)
        return SetInfo(
            set_name=set_name,
            persona_id=persona_id,
            has_finalized=has_finalized,
            finalized_mode=finalized_mode,
            finalized_checksum=finalized_checksum,
            has_wip=has_wip,
            wip_metadata=meta,
            wip_stages=stages,
            is_active=(active_name == set_name),
        )

    def _build_stage_state(
        self,
        persona_id: str,
        set_name: str,
        stage_id: str,
        meta: AvatarSetMetadata,
    ) -> StageState:
        sdir = self.stage_dir(persona_id, set_name, stage_id)
        files: list[StageOutputInfo] = []
        if sdir.exists():
            for entry in sorted(sdir.iterdir()):
                if entry.is_file() and entry.suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".webp"
                ):
                    files.append(
                        StageOutputInfo(
                            target=entry.stem,
                            path=str(entry),
                            size_bytes=entry.stat().st_size,
                        )
                    )
        return StageState(
            stage_id=stage_id,
            files=files,
            completed=(stage_id in meta.completed_stages),
        )


# ----- Module helpers -----


def _validate_id_component(value: str, field_name: str) -> None:
    """persona_id / set_name にパス区切り / 親参照 / 空文字が混入してないか検証。

    addon storage の `~/.saiverse/addons/<addon>/avatar_sets/` 配下に
    untrusted 入力で path を組み立てる以上、 traversal を防ぐ。
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"Invalid {field_name}: empty or non-string")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(
            f"Invalid {field_name}: contains path separator or parent ref: "
            f"{value!r}"
        )
    if value.startswith(".") or value.startswith("_"):
        # _active.json と衝突しないため、 set_name の先頭 "_" / "." を禁止。
        raise ValueError(
            f"Invalid {field_name}: must not start with '.' or '_': {value!r}"
        )
    # REST API の予約 path と衝突しないため、 一部の名前を禁止
    # (= /avatar_sets/{persona_id}/active / /avatar_sets/templates 等が
    # 後段の persona_id / set_name と被ると routing が壊れる)。
    _RESERVED_SET_NAMES = {"active"}
    _RESERVED_PERSONA_IDS = {"templates"}
    if field_name == "set_name" and value in _RESERVED_SET_NAMES:
        raise ValueError(
            f"Invalid {field_name}: {value!r} is reserved"
        )
    if field_name == "persona_id" and value in _RESERVED_PERSONA_IDS:
        raise ValueError(
            f"Invalid {field_name}: {value!r} is reserved"
        )


def _validate_stage_id(stage_id: str) -> None:
    if stage_id not in (
        STAGE_FACE, STAGE_EXPRESSIONS, STAGE_MATRIX, STAGE_LAYERED, STAGE_TRIMMED,
    ):
        raise ValueError(f"Unknown stage_id: {stage_id!r}")


def _stage_order_for_mode(mode: str) -> list[str]:
    if mode == "layered":
        return STAGE_ORDER_LAYERED
    return STAGE_ORDER_MATRIX


# ----- Singleton accessor -----

_singleton: Optional[AvatarPipelineManager] = None
_singleton_lock = threading.Lock()


def get_avatar_pipeline_manager() -> AvatarPipelineManager:
    """プロセス内シングルトンを返す。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AvatarPipelineManager()
    return _singleton


__all__ = [
    "ADDON_NAME",
    "STAGE_FACE",
    "STAGE_EXPRESSIONS",
    "STAGE_MATRIX",
    "STAGE_LAYERED",
    "STAGE_TRIMMED",
    "FACE_NAMES",
    "EXPRESSION_NAMES",
    "EYES_STATES",
    "MOUTH_SHAPES",
    "MATRIX_TOTAL_FRAMES",
    "VALID_MODES",
    "AvatarSetMetadata",
    "SetInfo",
    "StageState",
    "StageOutputInfo",
    "AvatarPipelineManager",
    "get_avatar_pipeline_manager",
]
