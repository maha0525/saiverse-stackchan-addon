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

interface AddonPanelProps {
    addon: {
        addon_name: string;
        display_name: string;
        version: string;
        description?: string;
    };
    personas: { id: string; name: string }[];
    addonApiBase: string;
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
    personas, addonApiBase,
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
            <AvatarSection
                personas={personas}
                addonApiBase={addonApiBase}
                debugMode={debugMode}
            />
            <DeviceSection addonApiBase={addonApiBase} />
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
                if (typeof data?.volume === "number") {
                    setVolume(data.volume);
                } else {
                    // volume key 不在 (raw fallback / firmware 仕様変更) は
                    // 50 を仮置きしてスライダだけ動かせるようにする。
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

const panelStyles: Record<string, React.CSSProperties> = {
    root: {
        padding: "12px",
        borderTop: "1px solid #333",
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
        color: "#aaa",
        cursor: "pointer",
    },
    section: {
        marginBottom: "12px",
        padding: "8px",
        background: "#1a1a1a",
        borderRadius: "4px",
    },
    sectionLabel: {
        fontSize: "12px",
        marginBottom: "4px",
        color: "#aaa",
        fontWeight: 600,
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
    },
    activeName: {
        color: "#dfd",
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
        background: "#222",
        color: "#eee",
        border: "1px solid #444",
        borderRadius: "3px",
    },
    label: { color: "#aaa", fontSize: "11px" },
    select: {
        padding: "3px 6px",
        background: "#222",
        color: "#eee",
        border: "1px solid #444",
        borderRadius: "3px",
        fontSize: "11px",
    },
    button: {
        padding: "4px 12px",
        fontSize: "12px",
        borderRadius: "3px",
        border: "1px solid #555",
        cursor: "pointer",
    },
    buttonPrimary: {
        padding: "4px 12px",
        background: "#264",
        color: "#fff",
        border: "1px solid #4a8",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonAccent: {
        padding: "4px 12px",
        background: "#246",
        color: "#cdf",
        border: "1px solid #48a",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonSubtle: {
        padding: "4px 12px",
        background: "#2a2a2a",
        color: "#aaa",
        border: "1px solid #444",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    buttonDisabled: {
        padding: "4px 12px",
        background: "#333",
        color: "#666",
        border: "1px solid #444",
        borderRadius: "3px",
        cursor: "not-allowed",
        fontSize: "11px",
    },
    errorBox: {
        marginBottom: "8px",
        padding: "6px",
        background: "#622",
        borderRadius: "4px",
        color: "#fcc",
        fontSize: "12px",
    },
    empty: {
        fontSize: "12px",
        color: "#666",
        padding: "8px",
        textAlign: "center",
    },
    setCard: {
        padding: "8px",
        marginBottom: "4px",
        background: "#1a1a1a",
        borderRadius: "4px",
        border: "1px solid #333",
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
        color: "#fff",
        fontSize: "12px",
    },
    activeBadge: {
        marginLeft: "6px",
        padding: "1px 6px",
        background: "#264",
        color: "#dfd",
        borderRadius: "3px",
        fontSize: "10px",
    },
    mode: {
        marginLeft: "6px",
        color: "#888",
        fontSize: "10px",
    },
    subtle: {
        color: "#888",
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
        background: "#522",
        color: "#fcc",
        border: "1px solid #844",
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
        color: "#ccc",
        fontSize: "11px",
        fontVariantNumeric: "tabular-nums",
    },
};
