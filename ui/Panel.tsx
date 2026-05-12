"use client";

/**
 * Stack-chan Vessel Addon - AddonManager 内表示パネル。
 *
 * frontend/scripts/sync-addon-panels.mjs により build 時に
 * frontend/src/addon-panels/saiverse-stackchan-addon/Panel.tsx へコピーされ、
 * AddonManagerModal の AddonCard 内で動的読み込みされる。
 *
 * Phase 1 機能:
 *   - 新規ペアリング (POST /pair)
 *   - 登録済み Vessel 一覧 (GET /vessels)
 *   - ペアリング解除 (DELETE /vessels/<id>)
 *   - 発行直後の vessel_id + device_token 表示 (一度しか取れないため強調)
 *
 * Phase 2 以降:
 *   - QR コード表示
 *   - ファームウェア書き込み (Web Serial フラッシュページへの遷移)
 */
import React, { useCallback, useEffect, useState } from "react";

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

interface Vessel {
    vessel_id: string;
    building_id: string;
    hardware_model: string;
    firmware_version: string | null;
    paired_at: string;
    last_seen_at: string | null;
    connected: boolean;
}

interface PairResult {
    vessel_id: string;
    device_token: string;
    building_id: string;
}

export default function StackchanVesselPanel({ addonApiBase }: AddonPanelProps) {
    const [vessels, setVessels] = useState<Vessel[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [newBuildingId, setNewBuildingId] = useState("");
    const [pairResult, setPairResult] = useState<PairResult | null>(null);

    const fetchVessels = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/vessels`);
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data = await res.json();
            setVessels(Array.isArray(data.vessels) ? data.vessels : []);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setLoading(false);
        }
    }, [addonApiBase]);

    useEffect(() => {
        fetchVessels();
    }, [fetchVessels]);

    const handlePair = async () => {
        const buildingId = newBuildingId.trim();
        if (!buildingId) return;
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/pair`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ building_id: buildingId }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data: PairResult = await res.json();
            setPairResult(data);
            setNewBuildingId("");
            await fetchVessels();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    const handleDelete = async (vesselId: string) => {
        const shortId = vesselId.substring(0, 8);
        if (!confirm(`Vessel ${shortId}... のペアリングを解除します。よろしいですか？`)) return;
        setError(null);
        try {
            const res = await fetch(`${addonApiBase}/vessels/${vesselId}`, {
                method: "DELETE",
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            await fetchVessels();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    return (
        <div style={panelStyles.root}>
            <h3 style={panelStyles.title}>Stack-chan ペアリング管理</h3>

            {/* 新規ペアリングフォーム */}
            <div style={panelStyles.section}>
                <div style={panelStyles.sectionLabel}>新規ペアリング</div>
                <div style={panelStyles.formRow}>
                    <input
                        type="text"
                        placeholder="Building ID (Vessel として使う建物の ID)"
                        value={newBuildingId}
                        onChange={(e) => setNewBuildingId(e.target.value)}
                        style={panelStyles.input}
                    />
                    <button
                        onClick={handlePair}
                        disabled={!newBuildingId.trim()}
                        style={{
                            ...panelStyles.button,
                            ...(newBuildingId.trim() ? panelStyles.buttonPrimary : panelStyles.buttonDisabled),
                        }}
                    >
                        ペアリング発行
                    </button>
                </div>
                <div style={panelStyles.hint}>
                    指定した Building は capacity=1 + PHYSICAL_VESSEL_ID を持つ Vessel Building になります。
                </div>
            </div>

            {/* 発行直後の token 表示 (一度しか取れない警告) */}
            {pairResult && (
                <div style={panelStyles.tokenBox}>
                    <div style={panelStyles.tokenBoxTitle}>
                        ペアリング情報 (この画面でしか表示されません — 安全な場所に控えてください)
                    </div>
                    <div style={panelStyles.tokenContent}>
                        <div>
                            <span style={panelStyles.tokenLabel}>vessel_id:</span>
                            <span style={panelStyles.tokenValue}>{pairResult.vessel_id}</span>
                        </div>
                        <div>
                            <span style={panelStyles.tokenLabel}>device_token:</span>
                            <span style={panelStyles.tokenValue}>{pairResult.device_token}</span>
                        </div>
                        <div>
                            <span style={panelStyles.tokenLabel}>building_id:</span>
                            <span style={panelStyles.tokenValue}>{pairResult.building_id}</span>
                        </div>
                    </div>
                    <button onClick={() => setPairResult(null)} style={panelStyles.tokenCloseBtn}>
                        閉じる
                    </button>
                </div>
            )}

            {/* エラー表示 */}
            {error && (
                <div style={panelStyles.errorBox}>
                    エラー: {error}
                </div>
            )}

            {/* 登録済み一覧 */}
            <div style={panelStyles.sectionLabel}>
                登録済み Vessel ({vessels.length}件) {loading && "読み込み中..."}
            </div>
            {vessels.length === 0 && !loading && (
                <div style={panelStyles.empty}>登録された Vessel はありません</div>
            )}
            {vessels.map((v) => (
                <div key={v.vessel_id} style={panelStyles.vesselCard}>
                    <div style={panelStyles.vesselCardRow}>
                        <div style={panelStyles.vesselInfo}>
                            <div>
                                <span style={{ color: v.connected ? "#4f8" : "#888", fontWeight: 600 }}>
                                    {v.connected ? "● 接続中" : "○ 切断中"}
                                </span>
                                {"  "}
                                <span style={panelStyles.mono}>vessel_id: {v.vessel_id.substring(0, 8)}...</span>
                            </div>
                            <div style={panelStyles.mono}>building_id: {v.building_id}</div>
                            <div style={panelStyles.mono}>hardware: {v.hardware_model}</div>
                            {v.firmware_version && (
                                <div style={panelStyles.mono}>firmware: {v.firmware_version}</div>
                            )}
                            <div style={panelStyles.subtle}>paired_at: {v.paired_at}</div>
                            {v.last_seen_at && (
                                <div style={panelStyles.subtle}>last_seen: {v.last_seen_at}</div>
                            )}
                        </div>
                        <button onClick={() => handleDelete(v.vessel_id)} style={panelStyles.deleteBtn}>
                            解除
                        </button>
                    </div>
                </div>
            ))}
        </div>
    );
}

// Phase 1 では CSS module を別途用意せず、インラインスタイルで最小実装。
// Phase 2 以降で .module.css に抽出する想定。
const panelStyles: Record<string, React.CSSProperties> = {
    root: {
        padding: "12px",
        borderTop: "1px solid #333",
        marginTop: "12px",
        fontSize: "12px",
    },
    title: {
        margin: "0 0 8px 0",
        fontSize: "14px",
        fontWeight: 600,
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
    },
    formRow: {
        display: "flex",
        gap: "6px",
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
    button: {
        padding: "4px 12px",
        fontSize: "12px",
        borderRadius: "3px",
        border: "1px solid #555",
        cursor: "pointer",
    },
    buttonPrimary: {
        background: "#264",
        color: "#fff",
    },
    buttonDisabled: {
        background: "#333",
        color: "#666",
        cursor: "not-allowed",
    },
    hint: {
        marginTop: "4px",
        fontSize: "11px",
        color: "#888",
    },
    tokenBox: {
        marginBottom: "12px",
        padding: "8px",
        background: "#264",
        borderRadius: "4px",
        border: "1px solid #4a8",
    },
    tokenBoxTitle: {
        marginBottom: "6px",
        fontWeight: 600,
        color: "#dfd",
    },
    tokenContent: {
        fontFamily: "monospace",
        fontSize: "11px",
    },
    tokenLabel: {
        color: "#9c9",
        marginRight: "4px",
    },
    tokenValue: {
        userSelect: "all",
        color: "#fff",
    },
    tokenCloseBtn: {
        marginTop: "6px",
        padding: "2px 8px",
        fontSize: "11px",
        background: "#1a3a2a",
        color: "#cfc",
        border: "1px solid #4a8",
        borderRadius: "3px",
        cursor: "pointer",
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
    vesselCard: {
        padding: "8px",
        marginBottom: "4px",
        background: "#1a1a1a",
        borderRadius: "4px",
    },
    vesselCardRow: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        gap: "8px",
    },
    vesselInfo: {
        flex: 1,
        fontSize: "11px",
        lineHeight: 1.5,
    },
    mono: {
        fontFamily: "monospace",
    },
    subtle: {
        color: "#888",
        fontSize: "10px",
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
};
