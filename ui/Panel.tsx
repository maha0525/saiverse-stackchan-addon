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
     * 再描画する。 ペアリング操作 (master_token rotate) で必須。
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
            />
            <FirmwareFlashSection addonApiBase={addonApiBase} />
            <AvatarSection
                personas={personas}
                addonApiBase={addonApiBase}
                debugMode={debugMode}
            />
            <DeviceSection addonApiBase={addonApiBase} />
        </div>
    );
}

// ----- Vessel Pairing section (Phase 2') -----
//
// Stack-chan device の登録・解除を addon UI から実行する。
// - POST /pair → device_token + vessel_id を発行、AddonConfig も自動更新
// - GET /vessels → 登録済み vessel 一覧
// - DELETE /vessels/{id} → 解除
// Phase 1' single vessel 前提に従い、1 機体が登録済みなら追加 UI は非表示。

interface VesselSummary {
    vessel_id: string;
    bound_building_id: string;
    bound_persona_id: string | null;
    hardware_model: string;
    firmware_version: string | null;
    paired_at: string;
    last_seen_at: string | null;
    connected: boolean;
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
    addonApiBase, onConfigChanged,
}: {
    addonApiBase: string;
    onConfigChanged?: () => void | Promise<void>;
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
            // AddonConfig.master_token / vessel_building_id を内部更新したので、
            // 親 (AddonManagerModal) に通知して ParamsSection を最新値で再描画。
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

    const canCreate = vessels !== null && vessels.length === 0;

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
                        </div>
                    ))}
                </div>
            )}

            {canCreate && (
                <div style={panelStyles.formRow}>
                    {buildings.length > 0 ? (
                        <select
                            value={selectedBuildingId}
                            onChange={(e) =>
                                setSelectedBuildingId(e.target.value)}
                            disabled={busy}
                            style={panelStyles.select}
                        >
                            <option value="">Building を選択…</option>
                            {buildings.map((b) => (
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

function fwInfoSourceLabel(source: FirmwareInfo["source"]): string {
    switch (source) {
        case "addon_config": return "AddonConfig 設定値";
        case "local_build": return "ローカルビルド (temp/stackchan-mcp/firmware/build)";
        case "user_default": return "ユーザー配置 (~/.saiverse/addons/...)";
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
    source: "addon_config" | "local_build" | "user_default" | "not_found";
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
                    onClick={fetchPorts}
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
                            ⚠ merged-binary.bin が見つかりません。
                            「ファームウェア書き込み」 は利用不可。<br />
                            <span style={panelStyles.subtle}>
                                配置 path 候補: (1) AddonConfig.firmware_path で
                                絶対 path 指定、 (2) &lt;SAIVerse repo&gt;
                                /temp/stackchan-mcp/firmware/build/、 (3)
                                ~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/
                            </span>
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

function DeviceSection({ addonApiBase }: { addonApiBase: string }) {
    // 音量: null = 初期 fetch 未完 / 失敗時は 50 fallback。 fetch 後はユーザー
    // 操作で更新し、 リリース時 (= onMouseUp / onTouchEnd) に POST する。
    const [volume, setVolume] = useState<number | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // マウント時 1 回だけ device 状態を fetch。 polling はしない (= 他経路で
    // 音量変わった場合は AddonManager を開き直すまで Panel の値はズレる、
    // が実害は次回操作で上書きされるだけ)。
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`${addonApiBase}/device/status`);
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
    }, [addonApiBase]);

    const commitVolume = async (v: number) => {
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/device/volume`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ volume: v }),
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
        setBusy(true);
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/device/leds/clear`, {
                method: "POST",
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

    return (
        <div style={panelStyles.section}>
            <div style={panelStyles.sectionLabel}>デバイス操作</div>

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
                    disabled={volume === null || busy}
                    style={panelStyles.slider}
                />
                <span style={panelStyles.volumeValue}>
                    {volume ?? "…"}
                </span>
            </div>

            <div style={panelStyles.row}>
                <button
                    onClick={clearLeds}
                    disabled={busy}
                    style={
                        busy
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
};
