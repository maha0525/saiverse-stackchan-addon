"use client";

/**
 * Stack-chan Vessel Addon - AddonManager 内表示パネル。
 *
 * frontend/scripts/sync-addon-panels.mjs により build 時に
 * frontend/src/addon-panels/saiverse-stackchan-addon/Panel.tsx へコピーされ、
 * AddonManagerModal の AddonCard 内で動的読み込みされる。
 *
 * 機能:
 *   - Vessel ペアリング管理 (Phase 1' 系、 v0.5 では archive 化、 後で復活)
 *   - Avatar 制作 (Phase 4.5-d-4): セット一覧 + 新規作成 + 削除 + アクティブ切替 +
 *     AvatarPipelineModal 起動
 *   - デバイス操作 (Phase 4.5-f): 音量スライダ + LED 全消灯。 ペルソナ spell
 *     とは別経路で、 ユーザーが直接デバイス状態を制御する用。
 */
import React, { useCallback, useEffect, useState } from "react";

import AvatarPipelineModal from "./AvatarPipelineModal";

// side-effect import: アドオン固有の CSS 変数 (--stackchan-X) を
// :global で document に登録する。 panelStyles の var(...) 参照で使う。
import "./theme.module.css";

interface AddonPanelProps {
    addon: {
        addon_name: string;
        display_name: string;
        version: string;
        description?: string;
    };
    personas: { id: string; name: string }[];
    addonApiBase: string;
    /**
     * Panel 内部で AddonConfig を書き換えた場合に呼ぶ callback。
     * 呼ぶと親 AddonManagerModal が再 fetch して ParamsSection を最新値で
     * 再描画する。 ペアリング操作 (1 台目のペアリングで master_token が
     * 新しく入る) のあとに呼ぶ。
     */
    onConfigChanged?: () => void | Promise<void>;
}

// ----- Avatar set types (= avatar_pipeline.py の SetInfo と対応) -----

interface AvatarSetInfo {
    set_name: string;
    persona_id: string;
    has_finalized: boolean;
    finalized_mode: string | null;
    finalized_checksum: string | null;
    has_wip: boolean;
    wip_metadata: {
        mode: string;
        common_prompt: string;
        completed_stages: string[];
    } | null;
    is_active: boolean;
}

interface ListSetsResponse {
    persona_id: string;
    active_set_name: string | null;
    sets: AvatarSetInfo[];
}

const DEBUG_FLAG_KEY = "stackchan-addon-debug-flag";

export default function StackchanVesselPanel({
    personas, addonApiBase, onConfigChanged,
}: AddonPanelProps) {
    const [debugMode, setDebugMode] = useState(false);
    // vessel 一覧が変わった (ペアリング追加 / 解除) ことを子セクション間で伝える
    // カウンタ。 VesselPairingSection が bump し、 DeviceSection がこれを依存に
    // 入れて再取得する (= 同一パネル内でペアリングした機体が「デバイス操作」の
    // 機体セレクタに即反映される)。
    const [vesselsRefreshKey, setVesselsRefreshKey] = useState(0);
    const bumpVessels = useCallback(
        () => setVesselsRefreshKey((k) => k + 1), [],
    );

    // localStorage から初期値復元。
    useEffect(() => {
        try {
            const stored = window.localStorage.getItem(DEBUG_FLAG_KEY);
            if (stored === "true") setDebugMode(true);
        } catch {
            // localStorage 不可な環境では default false。
        }
    }, []);

    const toggleDebug = () => {
        const next = !debugMode;
        setDebugMode(next);
        try {
            window.localStorage.setItem(DEBUG_FLAG_KEY, String(next));
        } catch {
            // ignore
        }
    };

    return (
        <div style={panelStyles.root}>
            <div style={panelStyles.titleRow}>
                <h3 style={panelStyles.title}>Stack-chan Vessel</h3>
                <label style={panelStyles.debugToggle}>
                    <input
                        type="checkbox"
                        checked={debugMode}
                        onChange={toggleDebug}
                    />
                    Debug
                </label>
            </div>
            <VesselPairingSection
                addonApiBase={addonApiBase}
                onConfigChanged={onConfigChanged}
                onVesselsChanged={bumpVessels}
            />
            <FirmwareFlashSection addonApiBase={addonApiBase} />
            <AvatarSection
                personas={personas}
                addonApiBase={addonApiBase}
                debugMode={debugMode}
            />
            <DeviceSection
                addonApiBase={addonApiBase}
                refreshKey={vesselsRefreshKey}
            />
        </div>
    );
}

// ----- Vessel Pairing section (Phase 2' / v0.10 マルチ機体) -----
//
// Stack-chan device の登録・解除・機体管理を addon UI から実行する。
// - POST /pair → device_token + vessel_id を発行、AddonConfig も自動更新
// - GET /vessels → 登録済み vessel 一覧 (ポート・接続先 URL・capability 込み)
// - DELETE /vessels/{id} → 解除
// - POST /vessels/{id}/capabilities → 搭載ユニット (capability) 手動設定
// v0.10 で複数機体に対応 (intent 設計 K)。 追加フォームは常に表示し、 既に
// ペアリング済みの Building は select から除外する。 機体ごとに per-vessel の
// ポート・接続先 URL を表示し、 搭載ユニットを capability トグルで設定する。

// ユニット配置 (docs/intent/stackchan_unit_placement.md)。同アドレスユニットを
// 別 channel に挿す構成 (ToF ×2 等) を表現する上位モデル。
interface UnitPlacement {
    type: string;
    channel: number | null; // ハブ経由なら 0-7、 直結なら null
    label: string;
}
interface HubConfig {
    type: "none" | "pahub";
    addr?: string; // "0x71" 形式 (pahub のみ)
}
interface UnitConfig {
    version?: number;
    hub: HubConfig;
    units: UnitPlacement[];
}

interface VesselSummary {
    vessel_id: string;
    bound_building_id: string;
    bound_persona_id: string | null;
    hardware_model: string;
    firmware_version: string | null;
    paired_at: string;
    last_seen_at: string | null;
    connected: boolean;
    // マルチ機体 (v0.10): per-vessel のポート・接続先 URL・capability。
    ws_port: number | null;
    capture_port: number | null;
    capabilities: Record<string, boolean>;
    // ユニット配置 (v0.11)。未設定なら null (= capabilities から初期表示を導出)。
    unit_config: UnitConfig | null;
    gateway_ws_url: string;
}

// 機体管理 UI で手動トグルする capability (= 搭載ユニット集合)。 key は
// backend の _KNOWN_CAPABILITIES / vessel_dispatch の cap_key と一致させる。
// label は「何も知らない人が分かる」表示名 (ユニット名 + 何のセンサーか)。
const CAPABILITY_OPTIONS: { key: string; label: string }[] = [
    { key: "env3", label: "環境センサー (ENV III: 温湿度・気圧)" },
    { key: "servo8", label: "8 サーボユニット (首・腕などの追加サーボ)" },
    { key: "sonic", label: "超音波距離センサー (RCWL-9620)" },
    { key: "tof", label: "ToF 距離センサー (VL53L1X: レーザー測距)" },
];

// ハブアドレス "0x71" → number。 不正なら null。
function parseHexAddr(s?: string): number | null {
    if (!s) return null;
    const n = /^0x/i.test(s) ? parseInt(s, 16) : parseInt(s, 10);
    return Number.isNaN(n) ? null : n;
}

// 編集初期状態: unit_config があればそれ、 無ければ capabilities から導出
// (docs/intent/stackchan_unit_placement.md §9 の fallback)。
function deriveInitialPlacement(vessel: VesselSummary): {
    hubType: "none" | "pahub";
    a0: boolean;
    a1: boolean;
    a2: boolean;
    units: UnitPlacement[];
} {
    const uc = vessel.unit_config;
    if (uc && Array.isArray(uc.units)) {
        const addr = uc.hub?.type === "pahub" ? parseHexAddr(uc.hub.addr) : null;
        return {
            hubType: uc.hub?.type === "pahub" ? "pahub" : "none",
            a0: addr != null ? (addr & 1) !== 0 : false,
            a1: addr != null ? (addr & 2) !== 0 : false,
            a2: addr != null ? (addr & 4) !== 0 : false,
            units: uc.units.map((u) => ({
                type: u.type,
                channel: typeof u.channel === "number" ? u.channel : null,
                label: u.label ?? "",
            })),
        };
    }
    const units = Object.entries(vessel.capabilities ?? {})
        .filter(([, on]) => on)
        .map(([type]) => ({ type, channel: null as number | null, label: "" }));
    return { hubType: "none", a0: false, a1: false, a2: false, units };
}

// 機体ごとのユニット配置エディタ (ハブ + channel + label)。 従来の capability
// トグルを置換。 保存で POST /vessels/{id}/unit-config → backend が検証 + 再登録。
function UnitPlacementEditor(props: {
    vessel: VesselSummary;
    addonApiBase: string;
    busy: boolean;
    setBusy: (b: boolean) => void;
    setError: (e: string | null) => void;
    onSaved: () => Promise<void> | void;
}): React.JSX.Element {
    const { vessel, addonApiBase, busy, setBusy, setError, onSaved } = props;
    const [ed, setEd] = useState(() => deriveInitialPlacement(vessel));
    const [saving, setSaving] = useState(false);

    const patch = (p: Partial<typeof ed>) => setEd((s) => ({ ...s, ...p }));
    const addUnit = () =>
        patch({
            units: [
                ...ed.units,
                { type: "tof", channel: ed.hubType === "pahub" ? 0 : null, label: "" },
            ],
        });
    const removeUnit = (i: number) =>
        patch({ units: ed.units.filter((_, idx) => idx !== i) });
    const updateUnit = (i: number, u: Partial<UnitPlacement>) =>
        patch({ units: ed.units.map((x, idx) => (idx === i ? { ...x, ...u } : x)) });

    const save = async () => {
        setBusy(true);
        setSaving(true);
        setError(null);
        try {
            const addr = 0x70 | ((ed.a2 ? 4 : 0) | (ed.a1 ? 2 : 0) | (ed.a0 ? 1 : 0));
            const unit_config = {
                version: 1,
                hub:
                    ed.hubType === "pahub"
                        ? { type: "pahub", addr: `0x${addr.toString(16)}` }
                        : { type: "none" },
                units: ed.units.map((u) => ({
                    type: u.type,
                    channel: ed.hubType === "pahub" ? u.channel ?? 0 : null,
                    label: u.label.trim(),
                })),
            };
            const res = await fetch(
                `${addonApiBase}/vessels/${encodeURIComponent(vessel.vessel_id)}/unit-config`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ unit_config }),
                },
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            await onSaved();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
            setSaving(false);
        }
    };

    return (
        <div style={panelStyles.capabilityBlock}>
            <div style={panelStyles.subtle}>搭載ユニット配置:</div>

            {/* ハブ設定 */}
            <div style={panelStyles.placementHubRow}>
                <label style={panelStyles.capabilityLabel}>
                    ハブ:{" "}
                    <select
                        value={ed.hubType}
                        disabled={busy}
                        onChange={(e) =>
                            patch({ hubType: e.target.value as "none" | "pahub" })}
                    >
                        <option value="none">なし (直結)</option>
                        <option value="pahub">PaHUB (I2C ハブ)</option>
                    </select>
                </label>
                {ed.hubType === "pahub" && (
                    <span style={panelStyles.placementAddr}>
                        アドレスパッド:
                        <label style={panelStyles.placementAddrPad}>
                            <input
                                type="checkbox"
                                checked={ed.a0}
                                disabled={busy}
                                onChange={(e) => patch({ a0: e.target.checked })}
                            />
                            A0
                        </label>
                        <label style={panelStyles.placementAddrPad}>
                            <input
                                type="checkbox"
                                checked={ed.a1}
                                disabled={busy}
                                onChange={(e) => patch({ a1: e.target.checked })}
                            />
                            A1
                        </label>
                        <label style={panelStyles.placementAddrPad}>
                            <input
                                type="checkbox"
                                checked={ed.a2}
                                disabled={busy}
                                onChange={(e) => patch({ a2: e.target.checked })}
                            />
                            A2
                        </label>
                    </span>
                )}
            </div>

            {/* ユニット行 */}
            {ed.units.length === 0 && (
                <div style={panelStyles.subtle}>(ユニット未登録)</div>
            )}
            {ed.units.map((u, i) => (
                <div key={i} style={panelStyles.placementUnitRow}>
                    <select
                        value={u.type}
                        disabled={busy}
                        onChange={(e) => updateUnit(i, { type: e.target.value })}
                    >
                        {CAPABILITY_OPTIONS.map((c) => (
                            <option key={c.key} value={c.key}>
                                {c.label}
                            </option>
                        ))}
                    </select>
                    {ed.hubType === "pahub" && (
                        <label style={panelStyles.placementCh}>
                            ch
                            <input
                                type="number"
                                min={0}
                                max={7}
                                disabled={busy}
                                value={u.channel ?? 0}
                                onChange={(e) =>
                                    updateUnit(i, { channel: Number(e.target.value) })}
                                style={panelStyles.placementChInput}
                            />
                        </label>
                    )}
                    <input
                        type="text"
                        placeholder="ラベル (例: 前方左)"
                        disabled={busy}
                        value={u.label}
                        onChange={(e) => updateUnit(i, { label: e.target.value })}
                        style={panelStyles.placementLabelInput}
                    />
                    <button
                        type="button"
                        disabled={busy}
                        onClick={() => removeUnit(i)}
                        style={panelStyles.placementRemoveBtn}
                        title="このユニットを外す"
                    >
                        ×
                    </button>
                </div>
            ))}

            <div style={panelStyles.placementActions}>
                <button
                    type="button"
                    disabled={busy}
                    onClick={addUnit}
                    style={panelStyles.placementAddBtn}
                >
                    + ユニット追加
                </button>
                <button
                    type="button"
                    disabled={busy || saving}
                    onClick={save}
                    style={panelStyles.saveBtn}
                >
                    {saving ? "保存中…" : "配置を保存"}
                </button>
            </div>
            <div style={panelStyles.subtle}>
                同じ種類を複数挿す場合はラベル必須・一意 (例: 前方左 / 前方右)。
                ラベルはペルソナが body_status で確認して呼び出しに使う。
            </div>
        </div>
    );
}

interface PairResponse {
    vessel_id: string;
    device_token: string;
    building_id: string;
    gateway_ws_url: string;
}

interface BuildingSummary {
    id: string;
    name: string;
}

function VesselPairingSection({
    addonApiBase, onConfigChanged, onVesselsChanged,
}: {
    addonApiBase: string;
    onConfigChanged?: () => void | Promise<void>;
    // ペアリング追加 / 解除で vessel 一覧が変わったことを親に通知する
    // (= 「デバイス操作」セクションの機体セレクタを即更新するため)。
    onVesselsChanged?: () => void;
}) {
    const [vessels, setVessels] = useState<VesselSummary[] | null>(null);
    const [buildings, setBuildings] = useState<BuildingSummary[]>([]);
    const [selectedBuildingId, setSelectedBuildingId] = useState<string>("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [newPairing, setNewPairing] = useState<PairResponse | null>(null);
    const [copied, setCopied] = useState(false);

    const fetchVessels = useCallback(async () => {
        try {
            const res = await fetch(`${addonApiBase}/vessels`);
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data = await res.json();
            setVessels(data.vessels ?? []);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            setVessels([]);
        }
    }, [addonApiBase]);

    // Building 一覧は addon の外 (= 本体 API /api/user/buildings) から取る。
    // 失敗時は手入力フォールバック (= input 欄が表示される)。
    const fetchBuildings = useCallback(async () => {
        try {
            const res = await fetch("/api/user/buildings");
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            setBuildings(data.buildings ?? []);
        } catch {
            // 取得失敗時は input fallback、 error 表示は出さない (= 本来の
            // operation エラーと混同しないため)
            setBuildings([]);
        }
    }, []);

    useEffect(() => {
        fetchVessels();
        fetchBuildings();
    }, [fetchVessels, fetchBuildings]);

    const createPairing = async () => {
        if (!selectedBuildingId.trim()) return;
        setBusy(true);
        setError(null);
        setNewPairing(null);
        try {
            const res = await fetch(`${addonApiBase}/pair`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    building_id: selectedBuildingId.trim(),
                }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data: PairResponse = await res.json();
            setNewPairing(data);
            setSelectedBuildingId("");
            await fetchVessels();
            onVesselsChanged?.();
            // ペアリングで AddonConfig.master_token を書いた (1 台目なら新しい値、
            // 2 台目以降は既存と同じ値) ので、 親 (AddonManagerModal) に通知して
            // ParamsSection を最新値で再描画。
            try {
                await onConfigChanged?.();
            } catch {
                // 親側 fetch 失敗は致命的じゃない、 ペアリング自体は成功扱い
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const deletePairing = async (vesselId: string, buildingId: string) => {
        const ok = window.confirm(
            `Building '${buildingId}' のペアリングを解除しますか?\n` +
            "device は再接続できなくなります。 再度ペアリングするには新規発行が必要です。",
        );
        if (!ok) return;
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(
                `${addonApiBase}/vessels/${encodeURIComponent(vesselId)}`,
                { method: "DELETE" },
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            setNewPairing(null);
            await fetchVessels();
            onVesselsChanged?.();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const copyToken = async () => {
        if (!newPairing) return;
        try {
            await navigator.clipboard.writeText(newPairing.device_token);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
        } catch {
            // clipboard 不可な環境 (= 非 secure context 等) は何もしない、
            // ユーザーは textarea から手動で選択コピーできる
        }
    };

    // 複数機体対応 (v0.10): single vessel ガードを撤廃。 追加フォームは常に
    // 出す。 ただし既にペアリング済みの Building は select から除外する
    // (= 同じ Building への二重ペアリングは backend が 409 で弾くので、 UX
    // 上あらかじめ選べないようにする)。
    const canCreate = vessels !== null;
    const boundBuildingIds = new Set(
        (vessels ?? []).map((v) => v.bound_building_id),
    );
    const availableBuildings = buildings.filter(
        (b) => !boundBuildingIds.has(b.id),
    );

    return (
        <div style={panelStyles.section}>
            <div style={panelStyles.sectionLabel}>Vessel ペアリング</div>

            {vessels === null ? (
                <div style={panelStyles.muted}>読み込み中…</div>
            ) : vessels.length === 0 ? (
                <div style={panelStyles.muted}>
                    ペアリング済みの Stack-chan はありません。
                </div>
            ) : (
                <div>
                    {vessels.map((v) => (
                        <div key={v.vessel_id} style={panelStyles.vesselCard}>
                            <div style={panelStyles.vesselCardRow}>
                                <div style={panelStyles.vesselDetails}>
                                    <div>
                                        <span style={panelStyles.vesselStatus}>
                                            {v.connected ? "🟢" : "⚪"}
                                        </span>
                                        <span style={panelStyles.vesselBuilding}>
                                            Building: {v.bound_building_id}
                                        </span>
                                    </div>
                                    <div style={panelStyles.subtle}>
                                        vessel_id: {v.vessel_id.slice(0, 8)}…
                                    </div>
                                    {v.bound_persona_id && (
                                        <div style={panelStyles.subtle}>
                                            persona: {v.bound_persona_id}
                                        </div>
                                    )}
                                    {v.firmware_version && (
                                        <div style={panelStyles.subtle}>
                                            fw: {v.firmware_version}
                                        </div>
                                    )}
                                    <div style={panelStyles.subtle}>
                                        paired: {formatPairedAt(v.paired_at)}
                                    </div>
                                    {(v.ws_port !== null
                                        || v.capture_port !== null) && (
                                        <div style={panelStyles.subtle}>
                                            ポート: ws {v.ws_port ?? "—"} /
                                            capture {v.capture_port ?? "—"}
                                        </div>
                                    )}
                                    <div style={panelStyles.subtle}>
                                        接続先:{" "}
                                        <code style={panelStyles.inlineCode}>
                                            {v.gateway_ws_url}
                                        </code>
                                    </div>
                                </div>
                                <button
                                    onClick={() => deletePairing(
                                        v.vessel_id, v.bound_building_id,
                                    )}
                                    disabled={busy}
                                    style={
                                        busy
                                            ? panelStyles.buttonDisabled
                                            : panelStyles.deleteBtn
                                    }
                                >
                                    解除
                                </button>
                            </div>

                            {/* 搭載ユニット配置 (ハブ + channel + label)。 ここで
                                登録した機体に降りたペルソナにだけ対応ユニット
                                ツールが見える。 同アドレスユニットを別 channel に
                                挿す構成 (ToF ×2 等) も表現できる
                                (docs/intent/stackchan_unit_placement.md)。 */}
                            <UnitPlacementEditor
                                vessel={v}
                                addonApiBase={addonApiBase}
                                busy={busy}
                                setBusy={setBusy}
                                setError={setError}
                                onSaved={fetchVessels}
                            />
                        </div>
                    ))}
                </div>
            )}

            {canCreate && (
                <div style={panelStyles.formRow}>
                    {availableBuildings.length > 0 ? (
                        <select
                            value={selectedBuildingId}
                            onChange={(e) =>
                                setSelectedBuildingId(e.target.value)}
                            disabled={busy}
                            style={panelStyles.select}
                        >
                            <option value="">Building を選択…</option>
                            {availableBuildings.map((b) => (
                                <option key={b.id} value={b.id}>
                                    {b.name} ({b.id})
                                </option>
                            ))}
                        </select>
                    ) : (
                        <input
                            type="text"
                            value={selectedBuildingId}
                            onChange={(e) =>
                                setSelectedBuildingId(e.target.value)}
                            placeholder="b_vessel_stackchan"
                            disabled={busy}
                            style={panelStyles.input}
                        />
                    )}
                    <button
                        onClick={createPairing}
                        disabled={busy || !selectedBuildingId.trim()}
                        style={
                            busy || !selectedBuildingId.trim()
                                ? panelStyles.buttonDisabled
                                : panelStyles.buttonPrimary
                        }
                    >
                        スタックチャンを追加
                    </button>
                </div>
            )}

            {newPairing && (
                <div style={panelStyles.pairingResult}>
                    <div style={panelStyles.pairingResultLabel}>
                        ペアリング発行しました。 device の captive portal で
                        以下を入力してください:
                    </div>
                    <div style={panelStyles.pairingKv}>
                        <span style={panelStyles.pairingKey}>
                            Gateway URL:
                        </span>
                        <code style={panelStyles.pairingValue}>
                            {newPairing.gateway_ws_url}
                        </code>
                    </div>
                    <div style={panelStyles.pairingKv}>
                        <span style={panelStyles.pairingKey}>Token:</span>
                        <code style={panelStyles.pairingValue}>
                            {newPairing.device_token}
                        </code>
                        <button
                            onClick={copyToken}
                            style={panelStyles.copyButton}
                        >
                            {copied ? "コピー済み" : "コピー"}
                        </button>
                    </div>
                    <div style={panelStyles.subtle}>
                        token は一度しか表示されません。 紛失したら DELETE で
                        解除して再ペアリングが必要です。
                    </div>
                </div>
            )}

            {error && (
                <div style={panelStyles.errorBox}>
                    エラー: {error}
                </div>
            )}
        </div>
    );
}

function formatPairedAt(iso: string): string {
    try {
        const d = new Date(iso);
        return d.toLocaleString();
    } catch {
        return iso;
    }
}

function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

// ファームウェアの入手先 = 本家 (kisaragi-mochi/stackchan-mcp) の配布ページ。
// merged-binary.bin が付いているのは、 名前が "firmware-" で始まるリリース
// だけ (ページの一番上に出る「最新」のリリースには付いていないことがある)。
// backend 側 (api_routes.py の _FIRMWARE_RELEASES_URL) にも同じ URL がある。
const FIRMWARE_RELEASES_URL =
    "https://github.com/kisaragi-mochi/stackchan-mcp/releases";

// ファームウェアの既定の置き場所 (= アドオンの永続データの中。 backend の
// _firmware_default_path() が返す場所)。
const FIRMWARE_DEFAULT_DIR =
    "~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/";

function fwInfoSourceLabel(source: FirmwareInfo["source"]): string {
    switch (source) {
        case "addon_config": return "アドオンの詳細設定で指定したファイル";
        case "user_default": return `既定の置き場所 (${FIRMWARE_DEFAULT_DIR})`;
        case "not_found": return "未検出";
    }
}

// ----- Firmware flash section (Phase 2' Step 4) -----
//
// Stack-chan device の NVS erase / firmware flash を UI から実行する。
// backend が esptool subprocess を起動して stdout を SSE で stream して
// くる、 EventSource で購読して append 表示。
//
// 2 ボタン:
//   - 「Wi-Fi 設定をリセット」 (内部的には NVS partition erase): 数秒、
//     ペアリング解除後の AP モード復帰用 (= device 単独で AP を立てて
//     captive portal を提供する状態に戻す)
//   - 「ファームウェア書き込み」: 初回 / クリーンインストール、 数分

interface FlashPort {
    port: string;
    description: string;
    vid: string | null;
    pid: string | null;
}

interface FirmwareInfo {
    path: string | null;
    exists: boolean;
    size: number | null;
    mtime_iso: string | null;
    source: "addon_config" | "user_default" | "not_found";
}

function FirmwareFlashSection({ addonApiBase }: { addonApiBase: string }) {
    const [ports, setPorts] = useState<FlashPort[]>([]);
    const [selectedPort, setSelectedPort] = useState<string>("");
    const [busy, setBusy] = useState(false);
    const [output, setOutput] = useState<string[]>([]);
    const [error, setError] = useState<string | null>(null);
    const [exitCode, setExitCode] = useState<number | null>(null);
    const [fwInfo, setFwInfo] = useState<FirmwareInfo | null>(null);

    const fetchFwInfo = useCallback(async () => {
        try {
            const res = await fetch(`${addonApiBase}/flash/firmware-info`);
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data: FirmwareInfo = await res.json();
            setFwInfo(data);
        } catch {
            // 取得失敗時は info 表示なし (= 致命的じゃない、 焼く時に
            // 別途エラーが出る)
            setFwInfo(null);
        }
    }, [addonApiBase]);

    const fetchPorts = useCallback(async () => {
        try {
            const res = await fetch(`${addonApiBase}/flash/ports`);
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data: FlashPort[] = await res.json();
            setPorts(data);
            // ESP32-S3 (VID 303A) を優先選択。 見つからなければ先頭。
            const esp = data.find((p) => p.vid === "303A");
            if (esp) {
                setSelectedPort(esp.port);
            } else if (data.length > 0 && !selectedPort) {
                setSelectedPort(data[0].port);
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    }, [addonApiBase, selectedPort]);

    useEffect(() => {
        fetchPorts();
        fetchFwInfo();
    }, [fetchPorts, fetchFwInfo]);

    // SSE 経路で esptool stdout を購読する。 完走 / エラーで Promise が
    // 解決される。 EventSource は GET しか送れないので fetch + stream
    // reader を使って POST + SSE-style response を扱う。
    const runFlash = async (endpoint: string, label: string) => {
        if (!selectedPort) {
            setError("COM port が選択されてません");
            return;
        }
        setBusy(true);
        setError(null);
        setExitCode(null);
        setOutput([`▶ ${label} (port=${selectedPort}) 開始…`]);
        try {
            const url = `${addonApiBase}${endpoint}?port=${encodeURIComponent(selectedPort)}`;
            const res = await fetch(url, { method: "POST" });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            if (!res.body) {
                throw new Error("Response body が空 (SSE stream なし)");
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let buffer = "";
            // eslint-disable-next-line no-constant-condition
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                // SSE event 区切り: 空行
                const parts = buffer.split("\n\n");
                buffer = parts.pop() ?? "";
                for (const part of parts) {
                    // `data: <json>` 形式
                    const line = part.trim();
                    if (!line.startsWith("data:")) continue;
                    const payload = line.slice(5).trim();
                    let event: { type: string; text?: string; returncode?: number };
                    try {
                        event = JSON.parse(payload);
                    } catch {
                        continue;
                    }
                    if (event.type === "line" && event.text) {
                        setOutput((prev) => [...prev, event.text!]);
                    } else if (event.type === "done") {
                        setExitCode(event.returncode ?? null);
                        setOutput((prev) => [
                            ...prev,
                            `\n✓ 完了 (returncode=${event.returncode})`,
                        ]);
                    } else if (event.type === "error" && event.text) {
                        setError(event.text);
                        setOutput((prev) => [
                            ...prev,
                            `\n✗ エラー: ${event.text}`,
                        ]);
                    }
                }
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const eraseNvs = async () => {
        const ok = window.confirm(
            "Stack-chan に保存された Wi-Fi 接続情報と認証情報を消去します。\n" +
            "完了後、 Stack-chan は自分で Wi-Fi スポットを立てて\n" +
            "セットアップ画面を出す状態 (初回起動と同じ) に戻ります。\n\n" +
            "続けますか?",
        );
        if (!ok) return;
        await runFlash("/flash/erase-nvs", "Wi-Fi 設定をリセット");
    };

    const flashFirmware = async () => {
        const ok = window.confirm(
            "Stack-chan のソフトウェア (ファームウェア) を書き込みます。\n" +
            "初回セットアップや、 動作がおかしくなった時の完全リセット用です。\n" +
            "保存された Wi-Fi 設定・認証情報はすべて消えます。 数分かかります。\n\n" +
            "続けますか?",
        );
        if (!ok) return;
        await runFlash("/flash/firmware", "ファームウェア書き込み");
    };

    return (
        <div style={panelStyles.section}>
            <div style={panelStyles.sectionLabel}>ファームウェア</div>

            <div style={panelStyles.row}>
                <label style={panelStyles.label}>COM port:</label>
                <select
                    value={selectedPort}
                    onChange={(e) => setSelectedPort(e.target.value)}
                    disabled={busy || ports.length === 0}
                    style={panelStyles.select}
                >
                    {ports.length === 0 && (
                        <option value="">(検出されません)</option>
                    )}
                    {ports.map((p) => (
                        <option key={p.port} value={p.port}>
                            {p.description}
                            {p.vid === "303A" ? " ⚡ESP32" : ""}
                        </option>
                    ))}
                </select>
                <button
                    onClick={() => {
                        // ファームウェアを手で置いたあとに警告を消せるよう、
                        // COM port と一緒にファームウェアの有無も調べ直す。
                        fetchPorts();
                        fetchFwInfo();
                    }}
                    disabled={busy}
                    style={busy ? panelStyles.buttonDisabled : panelStyles.buttonSubtle}
                >
                    再検出
                </button>
            </div>

            {fwInfo && (
                <div style={panelStyles.fwInfo}>
                    {fwInfo.source === "not_found" ? (
                        <div style={panelStyles.fwInfoMissing}>
                            ⚠ ファームウェア (merged-binary.bin) が見つかりません。
                            「ファームウェア書き込み」 は使えません。
                            <div style={panelStyles.subtle}>
                                通常は、 アドオンの導入時に自動でダウンロードされます。
                            </div>
                            <div style={panelStyles.subtle}>
                                手で置く場合は、{" "}
                                <a
                                    href={FIRMWARE_RELEASES_URL}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={panelStyles.fwInfoLink}
                                >
                                    {FIRMWARE_RELEASES_URL}
                                </a>
                                {" "}のページで、
                                名前が「firmware-」で始まるリリースに付いている
                                merged-binary.bin をダウンロードして、
                                次の場所に置いてください:{" "}
                                <code style={panelStyles.fwInfoPath}>
                                    {FIRMWARE_DEFAULT_DIR}merged-binary.bin
                                </code>
                            </div>
                            <div style={panelStyles.subtle}>
                                自分でビルドしたファームウェアを使う場合は、
                                アドオンの詳細設定の「ファームウェアのファイルの場所」で、
                                そのファイルを指定できます。
                                置いたあとは「再検出」を押すと、 この表示が更新されます。
                            </div>
                        </div>
                    ) : (
                        <>
                            <div>
                                <span style={panelStyles.fwInfoLabel}>
                                    使用する firmware:
                                </span>{" "}
                                <code style={panelStyles.fwInfoPath}>
                                    {fwInfo.path}
                                </code>
                            </div>
                            <div style={panelStyles.subtle}>
                                source: {fwInfoSourceLabel(fwInfo.source)}
                                {fwInfo.size !== null && (
                                    ` / size: ${formatBytes(fwInfo.size)}`
                                )}
                                {fwInfo.mtime_iso && (
                                    ` / mtime: ${formatPairedAt(fwInfo.mtime_iso)}`
                                )}
                            </div>
                        </>
                    )}
                </div>
            )}

            <div style={panelStyles.row}>
                <button
                    onClick={eraseNvs}
                    disabled={busy || !selectedPort}
                    title="Stack-chan の Wi-Fi 接続情報と認証情報を消して、 初回セットアップ画面 (Stack-chan 自身が Wi-Fi スポットを立てる状態) に戻します"
                    style={
                        busy || !selectedPort
                            ? panelStyles.buttonDisabled
                            : panelStyles.buttonAccent
                    }
                >
                    Wi-Fi 設定をリセット
                </button>
                <button
                    onClick={flashFirmware}
                    disabled={
                        busy || !selectedPort || fwInfo?.source === "not_found"
                    }
                    style={
                        busy || !selectedPort || fwInfo?.source === "not_found"
                            ? panelStyles.buttonDisabled
                            : panelStyles.deleteBtn
                    }
                >
                    ファームウェア書き込み
                </button>
            </div>

            {(output.length > 0 || busy) && (
                <pre style={panelStyles.flashOutput}>
                    {output.join("\n")}
                    {busy && <span style={panelStyles.flashBusyMarker}> ▌</span>}
                </pre>
            )}

            {exitCode !== null && exitCode !== 0 && !error && (
                <div style={panelStyles.errorBox}>
                    esptool が異常終了 (returncode={exitCode})
                </div>
            )}

            {error && (
                <div style={panelStyles.errorBox}>
                    エラー: {error}
                </div>
            )}
        </div>
    );
}

// ----- Device section (Phase 4.5-f) -----

function DeviceSection(
    { addonApiBase, refreshKey }: { addonApiBase: string; refreshKey: number },
) {
    // 対象機体 (複数機体対応、 intent K-7): デバイス操作は機体ごとに別 gateway
    // なので、 どの機体を操作するか選ぶ。 1 機体なら自動選択、 0 機体なら操作不可。
    const [vessels, setVessels] = useState<VesselSummary[] | null>(null);
    const [selectedVesselId, setSelectedVesselId] = useState<string>("");
    // 音量: null = 初期 fetch 未完 / 失敗時は 50 fallback。 fetch 後はユーザー
    // 操作で更新し、 リリース時 (= onMouseUp / onTouchEnd) に POST する。
    const [volume, setVolume] = useState<number | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    // 頭タッチセンサー: null = 初期 fetch 未完 / 取得失敗。 firmware は NVS で
    // 永続化された有効状態を返す (= #314)。 false にすると頭をなでても反応
    // しなくなる (= HandleTap / HandleStroke がローカル応答と event 送出の
    // 両方をスキップ)。
    const [touchEnabled, setTouchEnabled] = useState<boolean | null>(null);

    // 機体一覧を取得して選択肢にする。 1 機体なら自動選択、 既選択が消えたら
    // 先頭に付け替える。
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`${addonApiBase}/vessels`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (cancelled) return;
                const vs: VesselSummary[] = data.vessels ?? [];
                setVessels(vs);
                if (vs.length > 0) {
                    setSelectedVesselId((prev) =>
                        prev && vs.some((v) => v.vessel_id === prev)
                            ? prev : vs[0].vessel_id);
                } else {
                    setSelectedVesselId("");
                }
            } catch {
                if (!cancelled) setVessels([]);
            }
        })();
        return () => { cancelled = true; };
    }, [addonApiBase, refreshKey]);

    // 選択機体の device 状態 (音量) を fetch。 機体未選択ならスキップ。 polling は
    // しない (= 他経路で音量変わった場合は開き直すまでズレるが、 実害は次回操作で
    // 上書きされるだけ)。 機体を切り替えたら再 fetch。
    useEffect(() => {
        if (!selectedVesselId) { setVolume(null); return; }
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(
                    `${addonApiBase}/device/status?vessel_id=${encodeURIComponent(selectedVesselId)}`,
                );
                if (!res.ok) {
                    const body = await res.json().catch(() => null);
                    throw new Error(body?.detail ?? `HTTP ${res.status}`);
                }
                const data = await res.json();
                if (cancelled) return;
                // stackchan-mcp firmware (wifi_board.cc) は volume を
                // `audio_speaker.volume` の **ネスト構造** で返す。
                // トップレベル `volume` は旧形式 / raw fallback 想定で互換維持。
                const vol = (typeof data?.audio_speaker?.volume === "number")
                    ? data.audio_speaker.volume
                    : (typeof data?.volume === "number" ? data.volume : null);
                if (vol !== null) {
                    setVolume(vol);
                } else {
                    setVolume(50);
                }
            } catch (e) {
                if (!cancelled) {
                    setError(e instanceof Error ? e.message : String(e));
                    setVolume(50);
                }
            }
        })();
        return () => { cancelled = true; };
    }, [addonApiBase, selectedVesselId]);

    // 選択機体の頭タッチセンサー有効状態を fetch。 機体未選択ならスキップ。
    // 音量と同様 polling はしない。 取得失敗時は touchEnabled を null のままに
    // してトグルを無効化する (= 不定値で誤操作させない、 詳細は errorBox に出る)。
    useEffect(() => {
        if (!selectedVesselId) { setTouchEnabled(null); return; }
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(
                    `${addonApiBase}/device/touch-sensor?vessel_id=${encodeURIComponent(selectedVesselId)}`,
                );
                if (!res.ok) {
                    const body = await res.json().catch(() => null);
                    throw new Error(body?.detail ?? `HTTP ${res.status}`);
                }
                const data = await res.json();
                if (cancelled) return;
                if (typeof data?.enabled === "boolean") {
                    setTouchEnabled(data.enabled);
                } else {
                    setTouchEnabled(null);
                }
            } catch (e) {
                if (!cancelled) {
                    setError(e instanceof Error ? e.message : String(e));
                }
            }
        })();
        return () => { cancelled = true; };
    }, [addonApiBase, selectedVesselId]);

    const commitVolume = async (v: number) => {
        if (!selectedVesselId) return;
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/device/volume`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ volume: v, vessel_id: selectedVesselId }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const clearLeds = async () => {
        if (!selectedVesselId) return;
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(
                `${addonApiBase}/device/leds/clear?vessel_id=${encodeURIComponent(selectedVesselId)}`,
                { method: "POST" },
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const commitTouch = async (enabled: boolean) => {
        if (!selectedVesselId) return;
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/device/touch-sensor`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled, vessel_id: selectedVesselId }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            // 成功して初めて state を更新 (= 楽観更新しない、 失敗時に
            // トグルが実機状態とズレないように)。
            setTouchEnabled(enabled);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div style={panelStyles.section}>
            <div style={panelStyles.sectionLabel}>デバイス操作</div>

            {/* 機体セレクタ (複数機体対応、 intent K-7)。 操作は選択中の機体に
                振り分けられる。 0 機体なら操作不可、 1 機体なら表示のみ。 */}
            {vessels !== null && vessels.length === 0 ? (
                <div style={panelStyles.muted}>
                    ペアリング済みの Stack-chan がありません。
                </div>
            ) : vessels && vessels.length > 1 ? (
                <div style={panelStyles.row}>
                    <label style={panelStyles.label}>機体:</label>
                    <select
                        value={selectedVesselId}
                        onChange={(e) => setSelectedVesselId(e.target.value)}
                        disabled={busy}
                        style={panelStyles.select}
                    >
                        {vessels.map((v) => (
                            <option key={v.vessel_id} value={v.vessel_id}>
                                {v.bound_building_id} ({v.vessel_id.slice(0, 8)}…)
                                {v.connected ? " 🟢" : " ⚪"}
                            </option>
                        ))}
                    </select>
                </div>
            ) : vessels && vessels.length === 1 ? (
                <div style={panelStyles.subtle}>
                    機体: {vessels[0].bound_building_id}
                </div>
            ) : null}

            <div style={panelStyles.row}>
                <label style={panelStyles.label}>音量:</label>
                <input
                    type="range"
                    min={0}
                    max={100}
                    value={volume ?? 0}
                    onChange={(e) => setVolume(Number(e.target.value))}
                    onMouseUp={(e) =>
                        commitVolume(Number((e.target as HTMLInputElement).value))}
                    onTouchEnd={(e) =>
                        commitVolume(Number((e.target as HTMLInputElement).value))}
                    disabled={volume === null || busy || !selectedVesselId}
                    style={panelStyles.slider}
                />
                <span style={panelStyles.volumeValue}>
                    {volume ?? "…"}
                </span>
            </div>

            <div style={panelStyles.row}>
                <label
                    style={{
                        ...panelStyles.label,
                        display: "flex",
                        alignItems: "center",
                        gap: "6px",
                        cursor: (touchEnabled === null || busy || !selectedVesselId)
                            ? "not-allowed" : "pointer",
                    }}
                >
                    <input
                        type="checkbox"
                        checked={touchEnabled === true}
                        onChange={(e) => commitTouch(e.target.checked)}
                        disabled={touchEnabled === null || busy || !selectedVesselId}
                    />
                    頭タッチセンサー
                    <span style={panelStyles.subtle}>
                        {touchEnabled === null
                            ? "（状態取得中…）"
                            : touchEnabled ? "（有効）" : "（無効）"}
                    </span>
                </label>
            </div>
            <div style={{ ...panelStyles.subtle, marginBottom: "6px" }}>
                OFF にすると頭をなでても反応しなくなります（誤作動対策・
                再起動後も保持されます）。
            </div>

            <div style={panelStyles.row}>
                <button
                    onClick={clearLeds}
                    disabled={busy || !selectedVesselId}
                    style={
                        busy || !selectedVesselId
                            ? panelStyles.buttonDisabled
                            : panelStyles.buttonSubtle
                    }
                >
                    LED 全消灯
                </button>
            </div>

            {error && (
                <div style={panelStyles.errorBox}>
                    エラー: {error}
                </div>
            )}
        </div>
    );
}

// ----- Avatar section -----

function AvatarSection({
    personas, addonApiBase, debugMode,
}: {
    personas: { id: string; name: string }[];
    addonApiBase: string;
    debugMode: boolean;
}) {
    const [selectedPersona, setSelectedPersona] = useState<string>(
        personas[0]?.id ?? "",
    );
    const [sets, setSets] = useState<AvatarSetInfo[]>([]);
    const [activeSetName, setActiveSetName] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [openModalSet, setOpenModalSet] = useState<string | null>(null);
    const [newSetName, setNewSetName] = useState("");
    const [newSetMode, setNewSetMode] = useState<"matrix" | "layered">(
        "matrix",
    );
    const [busy, setBusy] = useState(false);

    const fetchSets = useCallback(async () => {
        if (!selectedPersona) return;
        setError(null);
        try {
            const res = await fetch(
                `${addonApiBase}/avatar_sets/${encodeURIComponent(selectedPersona)}`,
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data: ListSetsResponse = await res.json();
            setSets(data.sets);
            setActiveSetName(data.active_set_name);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    }, [addonApiBase, selectedPersona]);

    useEffect(() => { fetchSets(); }, [fetchSets]);

    const createSet = async () => {
        if (!newSetName.trim() || !selectedPersona) return;
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(
                `${addonApiBase}/avatar_sets/${encodeURIComponent(selectedPersona)}/${encodeURIComponent(newSetName.trim())}`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ mode: newSetMode }),
                },
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            setNewSetName("");
            await fetchSets();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const deleteSet = async (setName: string, wipOnly: boolean) => {
        const label = wipOnly ? `${setName} の WIP を削除` : `${setName} 全体を削除`;
        if (!confirm(`${label} します。 よろしいですか？`)) return;
        setBusy(true);
        setError(null);
        try {
            const url = wipOnly
                ? `${addonApiBase}/avatar_sets/${encodeURIComponent(selectedPersona)}/${encodeURIComponent(setName)}?wip_only=true`
                : `${addonApiBase}/avatar_sets/${encodeURIComponent(selectedPersona)}/${encodeURIComponent(setName)}`;
            const res = await fetch(url, { method: "DELETE" });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            await fetchSets();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const setActive = async (setName: string | null) => {
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(
                `${addonApiBase}/avatar_sets/${encodeURIComponent(selectedPersona)}/active`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ set_name: setName }),
                },
            );
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            await fetchSets();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const selectedPersonaName =
        personas.find((p) => p.id === selectedPersona)?.name ?? selectedPersona;

    return (
        <div style={panelStyles.section}>
            <div style={panelStyles.sectionLabel}>Avatar 制作</div>

            {/* ペルソナ選択 */}
            <div style={panelStyles.row}>
                <label style={panelStyles.label}>ペルソナ:</label>
                <select
                    value={selectedPersona}
                    onChange={(e) => setSelectedPersona(e.target.value)}
                    style={panelStyles.select}
                >
                    {personas.map((p) => (
                        <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                </select>
            </div>

            {/* 新規作成 */}
            <div style={panelStyles.formRow}>
                <input
                    type="text"
                    placeholder="新規セット名 (例: default, yukata, short-hair)"
                    value={newSetName}
                    onChange={(e) => setNewSetName(e.target.value)}
                    style={panelStyles.input}
                />
                <select
                    value={newSetMode}
                    onChange={(e) =>
                        setNewSetMode(e.target.value as "matrix" | "layered")}
                    style={panelStyles.select}
                >
                    <option value="matrix">matrix (90枚)</option>
                    <option value="layered">layered (14枚)</option>
                </select>
                <button
                    onClick={createSet}
                    disabled={!newSetName.trim() || busy}
                    style={
                        !newSetName.trim() || busy
                            ? panelStyles.buttonDisabled
                            : panelStyles.buttonPrimary
                    }
                >
                    作成
                </button>
            </div>

            {error && (
                <div style={panelStyles.errorBox}>
                    エラー: {error}
                </div>
            )}

            {/* セット一覧 */}
            <div style={panelStyles.sectionLabel}>
                セット一覧 ({sets.length}件)
                {activeSetName && (
                    <span style={panelStyles.activeName}>
                        active: {activeSetName}
                    </span>
                )}
            </div>
            {sets.length === 0 && (
                <div style={panelStyles.empty}>
                    まだセットがありません。 上の入力欄から作成してください。
                </div>
            )}
            {sets.map((s) => (
                <div key={s.set_name} style={panelStyles.setCard}>
                    <div style={panelStyles.setCardRow}>
                        <div style={panelStyles.setInfo}>
                            <div>
                                <span style={panelStyles.setName}>
                                    {s.set_name}
                                </span>
                                {s.is_active && (
                                    <span style={panelStyles.activeBadge}>
                                        active
                                    </span>
                                )}
                                <span style={panelStyles.mode}>
                                    {s.wip_metadata?.mode ?? s.finalized_mode}
                                </span>
                            </div>
                            <div style={panelStyles.subtle}>
                                {s.has_finalized
                                    ? `確定品: ${s.finalized_checksum?.slice(0, 16)}...`
                                    : "未確定 (まだ avatar.bin なし)"}
                            </div>
                            <div style={panelStyles.subtle}>
                                WIP 段階: {s.wip_metadata?.completed_stages.join(", ") || "なし"}
                            </div>
                        </div>
                        <div style={panelStyles.setActions}>
                            <button
                                onClick={() => setOpenModalSet(s.set_name)}
                                style={panelStyles.buttonPrimary}
                            >
                                開く
                            </button>
                            {!s.is_active && s.has_finalized && (
                                <button
                                    onClick={() => setActive(s.set_name)}
                                    disabled={busy}
                                    style={panelStyles.buttonAccent}
                                >
                                    アクティブにする
                                </button>
                            )}
                            {s.has_wip && (
                                <button
                                    onClick={() => deleteSet(s.set_name, true)}
                                    disabled={busy}
                                    style={panelStyles.buttonSubtle}
                                >
                                    WIP のみ削除
                                </button>
                            )}
                            <button
                                onClick={() => deleteSet(s.set_name, false)}
                                disabled={busy}
                                style={panelStyles.deleteBtn}
                            >
                                削除
                            </button>
                        </div>
                    </div>
                </div>
            ))}

            {/* Modal */}
            {openModalSet && (
                <AvatarPipelineModal
                    addonApiBase={addonApiBase}
                    personaId={selectedPersona}
                    personaName={selectedPersonaName}
                    setName={openModalSet}
                    debugMode={debugMode}
                    onClose={() => setOpenModalSet(null)}
                    onChanged={fetchSets}
                />
            )}
        </div>
    );
}

// ----- Inline styles -----

// 色は本体 globals.css の --bg-X / --text-X / --border-color と、
// アドオン固有の --stackchan-X (theme.module.css 参照) を使い、
// light / dark テーマ切替に追従する。
const panelStyles: Record<string, React.CSSProperties> = {
    root: {
        padding: "12px",
        borderTop: "1px solid var(--border-color)",
        marginTop: "12px",
        fontSize: "12px",
    },
    title: {
        margin: 0,
        fontSize: "14px",
        fontWeight: 600,
    },
    titleRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: "8px",
    },
    debugToggle: {
        display: "flex",
        alignItems: "center",
        gap: "4px",
        fontSize: "11px",
        color: "var(--text-secondary)",
        cursor: "pointer",
    },
    section: {
        marginBottom: "12px",
        padding: "8px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
    },
    sectionLabel: {
        fontSize: "12px",
        marginBottom: "4px",
        color: "var(--text-secondary)",
        fontWeight: 600,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
    },
    activeName: {
        color: "var(--stackchan-success-soft-fg)",
        fontWeight: 400,
        fontSize: "11px",
    },
    row: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        marginBottom: "6px",
        flexWrap: "wrap",
    },
    formRow: {
        display: "flex",
        gap: "6px",
        marginBottom: "6px",
    },
    input: {
        flex: 1,
        padding: "4px 6px",
        fontSize: "12px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
    },
    label: { color: "var(--text-secondary)", fontSize: "11px" },
    select: {
        padding: "3px 6px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontSize: "11px",
    },
    button: {
        padding: "4px 12px",
        fontSize: "12px",
        borderRadius: "3px",
        border: "1px solid var(--border-color)",
        cursor: "pointer",
    },
    buttonPrimary: {
        padding: "4px 12px",
        background: "var(--stackchan-success-strong-bg)",
        color: "var(--stackchan-success-strong-fg)",
        border: "1px solid var(--stackchan-success-border)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonAccent: {
        padding: "4px 12px",
        background: "var(--stackchan-info-soft-bg)",
        color: "var(--stackchan-info-soft-fg)",
        border: "1px solid var(--stackchan-info-border)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonSubtle: {
        padding: "4px 12px",
        background: "var(--bg-hover)",
        color: "var(--text-secondary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonDisabled: {
        padding: "4px 12px",
        background: "var(--bg-tertiary)",
        color: "var(--text-secondary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        cursor: "not-allowed",
        fontSize: "11px",
        opacity: 0.6,
    },
    errorBox: {
        marginBottom: "8px",
        padding: "6px",
        background: "var(--stackchan-danger-soft-bg)",
        borderRadius: "4px",
        color: "var(--stackchan-danger-soft-fg)",
        fontSize: "12px",
    },
    empty: {
        fontSize: "12px",
        color: "var(--text-secondary)",
        padding: "8px",
        textAlign: "center",
    },
    setCard: {
        padding: "8px",
        marginBottom: "4px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    setCardRow: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        gap: "8px",
    },
    setInfo: {
        flex: 1,
        fontSize: "11px",
        lineHeight: 1.5,
    },
    setName: {
        fontWeight: 600,
        color: "var(--text-primary)",
        fontSize: "12px",
    },
    activeBadge: {
        marginLeft: "6px",
        padding: "1px 6px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    mode: {
        marginLeft: "6px",
        color: "var(--text-secondary)",
        fontSize: "10px",
    },
    subtle: {
        color: "var(--text-secondary)",
        fontSize: "10px",
    },
    setActions: {
        display: "flex",
        flexDirection: "column",
        gap: "4px",
    },
    deleteBtn: {
        padding: "4px 10px",
        fontSize: "11px",
        background: "var(--stackchan-danger-strong-bg)",
        color: "var(--stackchan-danger-soft-fg)",
        border: "1px solid var(--stackchan-danger-border)",
        borderRadius: "3px",
        cursor: "pointer",
    },
    slider: {
        flex: 1,
        cursor: "pointer",
    },
    volumeValue: {
        minWidth: "28px",
        textAlign: "right",
        color: "var(--text-primary)",
        fontSize: "11px",
        fontVariantNumeric: "tabular-nums",
    },
    // ----- Vessel Pairing -----
    muted: {
        color: "var(--text-secondary)",
        fontSize: "11px",
        padding: "4px 0",
    },
    vesselCard: {
        padding: "8px",
        marginBottom: "6px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    vesselCardRow: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        gap: "8px",
    },
    vesselDetails: {
        flex: 1,
        fontSize: "11px",
        lineHeight: 1.5,
    },
    vesselStatus: {
        marginRight: "4px",
    },
    vesselBuilding: {
        fontWeight: 600,
        color: "var(--text-primary)",
        fontSize: "12px",
    },
    inlineCode: {
        padding: "1px 4px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        fontFamily: "monospace",
        fontSize: "10px",
        borderRadius: "2px",
        wordBreak: "break-all",
    },
    capabilityBlock: {
        marginTop: "6px",
        paddingTop: "6px",
        borderTop: "1px solid var(--border-color)",
    },
    capabilityRow: {
        display: "flex",
        flexDirection: "column",
        gap: "3px",
        marginTop: "3px",
    },
    capabilityLabel: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        fontSize: "11px",
        color: "var(--text-secondary)",
    },
    placementHubRow: {
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: "10px",
        marginTop: "4px",
        fontSize: "11px",
        color: "var(--text-secondary)",
    },
    placementAddr: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        fontSize: "11px",
        color: "var(--text-secondary)",
    },
    placementAddrPad: {
        display: "flex",
        alignItems: "center",
        gap: "3px",
    },
    placementUnitRow: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        marginTop: "4px",
    },
    placementCh: {
        display: "flex",
        alignItems: "center",
        gap: "3px",
        fontSize: "11px",
        color: "var(--text-secondary)",
    },
    placementChInput: {
        width: "44px",
        fontSize: "11px",
        padding: "2px 4px",
    },
    placementLabelInput: {
        flex: 1,
        minWidth: "80px",
        fontSize: "11px",
        padding: "2px 6px",
    },
    placementRemoveBtn: {
        padding: "2px 8px",
        fontSize: "12px",
        lineHeight: 1,
        background: "var(--stackchan-danger-strong-bg)",
        color: "var(--stackchan-danger-soft-fg)",
        border: "1px solid var(--stackchan-danger-border)",
        borderRadius: "3px",
        cursor: "pointer",
    },
    placementActions: {
        display: "flex",
        alignItems: "center",
        gap: "8px",
        marginTop: "8px",
    },
    placementAddBtn: {
        padding: "4px 10px",
        fontSize: "11px",
        background: "var(--stackchan-panel-bg, transparent)",
        color: "var(--text-secondary)",
        border: "1px dashed var(--border-color)",
        borderRadius: "3px",
        cursor: "pointer",
    },
    saveBtn: {
        padding: "4px 12px",
        fontSize: "11px",
        background: "var(--stackchan-success-strong-bg, var(--accent-color))",
        color: "var(--stackchan-success-soft-fg, #fff)",
        border: "1px solid var(--stackchan-success-border, var(--accent-color))",
        borderRadius: "3px",
        cursor: "pointer",
    },
    pairingResult: {
        marginTop: "8px",
        padding: "8px",
        background: "var(--stackchan-success-soft-bg)",
        border: "1px solid var(--stackchan-success-border)",
        borderRadius: "4px",
        fontSize: "11px",
    },
    pairingResultLabel: {
        marginBottom: "6px",
        color: "var(--stackchan-success-soft-fg)",
        fontWeight: 600,
    },
    pairingKv: {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        marginBottom: "4px",
        flexWrap: "wrap",
    },
    pairingKey: {
        color: "var(--text-secondary)",
        minWidth: "78px",
    },
    pairingValue: {
        // code block 系は light でもターミナル風に暗背景を保つ
        flex: 1,
        padding: "2px 6px",
        background: "var(--stackchan-code-bg)",
        color: "var(--stackchan-code-token-fg)",
        fontFamily: "monospace",
        fontSize: "11px",
        borderRadius: "3px",
        wordBreak: "break-all",
    },
    copyButton: {
        padding: "2px 8px",
        fontSize: "10px",
        background: "var(--stackchan-info-soft-bg)",
        color: "var(--stackchan-info-soft-fg)",
        border: "1px solid var(--stackchan-info-border)",
        borderRadius: "3px",
        cursor: "pointer",
    },
    // ----- Firmware flash -----
    flashOutput: {
        marginTop: "6px",
        padding: "6px 8px",
        background: "var(--stackchan-code-bg)",
        color: "var(--stackchan-code-success-fg)",
        fontFamily: "monospace",
        fontSize: "10px",
        lineHeight: 1.4,
        borderRadius: "3px",
        border: "1px solid var(--border-color)",
        maxHeight: "240px",
        overflowY: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
    },
    flashBusyMarker: {
        color: "var(--stackchan-warning-fg)",
        fontWeight: 700,
    },
    fwInfo: {
        marginBottom: "6px",
        padding: "6px 8px",
        background: "var(--bg-secondary)",
        borderRadius: "3px",
        border: "1px solid var(--border-color)",
        fontSize: "11px",
        lineHeight: 1.5,
    },
    fwInfoLabel: {
        color: "var(--text-secondary)",
    },
    fwInfoPath: {
        padding: "1px 4px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        fontFamily: "monospace",
        fontSize: "10px",
        borderRadius: "2px",
        wordBreak: "break-all",
    },
    fwInfoMissing: {
        color: "var(--stackchan-warning-fg)",
    },
    fwInfoLink: {
        color: "var(--stackchan-info-soft-fg)",
        wordBreak: "break-all",
    },
};
