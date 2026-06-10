"use client";

/**
 * AvatarPipelineModal - Phase 4.5-d-4 (UI 統合)
 *
 * Panel.tsx の「Avatar 制作」 ボタンから起動される modal。 6 段階の
 * Wizard 形式で avatar セットを構築する:
 *   ① 元顔生成 → ② 表情差分 5 種 → ③ 目・口差分 → ④ トリミング →
 *   ⑤ 確定化 (avatar.bin 書き出し) → ⑥ Stack-chan 転送
 *
 * 各段階は addon API (= /api/addon/saiverse-stackchan-addon/avatar_sets/...)
 * を叩いて結果を取得、 画像プレビューは /files/{stage}/{filename}.png で取る。
 *
 * 詳細設計: docs/intent/stackchan_avatar_pipeline.md §D-3〜D-7
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

// side-effect import: アドオン固有の CSS 変数 (--stackchan-X) を
// :global で document に登録する。 styles の var(...) 参照で使う。
// Panel.tsx でも import しているので重複読み込みは Next.js 側で de-dup される。
import "./theme.module.css";

// ----- Types (= avatar_pipeline.py の dataclass と対応) -----

interface SetMetadata {
    version: number;
    mode: "matrix" | "layered";
    common_prompt: string;
    extra_prompts: Record<string, Record<string, string>>;
    trim_rect: TrimRect | null;
    trim_rect_overrides: Record<string, TrimRect>;
    parallelism: number;
    image_model: string;
    image_quality: string;
    aspect_ratio: string;
    stage_quality_overrides: Record<string, string>;
    stage_aspect_overrides: Record<string, string>;
    apply_common_prompt_to_stage3: boolean;
    completed_stages: string[];
    current_stage: string;
    created_at: string;
    updated_at: string;
}

interface StageFile {
    target: string;
    path: string;
    size_bytes: number;
}

interface StageState {
    stage_id: string;
    completed: boolean;
    files: StageFile[];
}

interface SetInfo {
    set_name: string;
    persona_id: string;
    has_finalized: boolean;
    finalized_mode: string | null;
    finalized_checksum: string | null;
    has_wip: boolean;
    wip_metadata: SetMetadata | null;
    wip_stages: StageState[];
    is_active: boolean;
}

interface AvatarPipelineModalProps {
    addonApiBase: string;
    personaId: string;
    personaName: string;
    setName: string;
    debugMode: boolean;
    onClose: () => void;
    onChanged?: () => void;
}

// ----- Constants (= avatar_pipeline.py と一致) -----

const STAGE_LABELS: Record<string, string> = {
    "01_face": "① 元顔",
    "02_expressions": "② 表情差分",
    "03_matrix": "③ 目・口差分",
    "03_layered": "③ 目・口差分",
    "04_trimmed": "④ トリミング",
};

const STAGE_DESCRIPTIONS: Record<string, string> = {
    "01_face":
        "ベースになる「目を開けて口を閉じた」 1 枚を作る。生成 or 既存画像のアップロード。",
    "02_expressions":
        "idle 以外の 5 表情 (happy / thinking / sad / surprised / embarrassed) のベース画像を作る。",
    "03_matrix":
        "6 表情 × 3 目状態 × 5 口形状 = 84 枚をまとめて作る。眼開・口閉の 6 枚は ①② から自動コピー。",
    "03_layered":
        "目 3 枚 + 口 5 枚 = 8 枚のレイヤーを作る。実機で face レイヤーに重ねて表示。",
    "04_trimmed":
        "全画像を 160×120 にトリム。avatar.bin に出力して Vessel 内なら自動転送。",
};

const FACE_NAMES = [
    "idle", "happy", "thinking", "sad", "surprised", "embarrassed",
];
const EXPRESSION_NAMES = [
    "happy", "thinking", "sad", "surprised", "embarrassed",
];
const EYES_STATES = ["open", "half", "closed"];
const MOUTH_SHAPES = ["closed", "half", "open", "e", "u"];
// 並び順 = dropdown 表示順 + 新規セット作成時の初期値 (= 配列先頭)。
// backend default も "gpt_image_2" (avatar_pipeline.py:127)。
const IMAGE_MODELS = [
    "gpt_image_2", "gpt_image_1_5",
    "nano_banana_2", "nano_banana_pro", "grok_imagine",
];
const IMAGE_QUALITIES = ["low", "medium", "high", "auto"];
const SUPPORTED_ASPECTS = [
    "1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "4:5", "5:4",
];

/** 画像サイズの中に収まる最大の中央矩形を、 指定アス比で計算する。 「4:3
 *  固定」 が ON のまま 4:3 じゃない画像をアップロードした時、 初期 trim
 *  矩形が元画像比率になってしまう問題を避けるための共通計算。 */
function fitRectToAspect(
    imgW: number, imgH: number, aspect: string,
): { x: number; y: number; width: number; height: number } {
    const [aw, ah] = aspect.split(":").map(Number);
    if (!aw || !ah) return { x: 0, y: 0, width: imgW, height: imgH };
    const targetRatio = aw / ah;
    const imgRatio = imgW / imgH;
    if (imgRatio > targetRatio) {
        // 画像が target より横長 → 高さ最大、 幅を縮めて中央配置
        const rectW = Math.round(imgH * targetRatio);
        return {
            x: Math.round((imgW - rectW) / 2),
            y: 0,
            width: rectW,
            height: imgH,
        };
    }
    // 画像が target より縦長 (or 同じ) → 幅最大、 高さを縮めて中央配置
    const rectH = Math.round(imgW / targetRatio);
    return {
        x: 0,
        y: Math.round((imgH - rectH) / 2),
        width: imgW,
        height: rectH,
    };
}

/** 矩形の実アス比に最も近い対応アス比を返す (= backend
 *  avatar_finalizer.closest_supported_aspect と同じロジック)。 ① の
 *  crop 矩形が真実で、 セレクト表示と ②③ 生成アス比をこれに同期する。 */
function closestSupportedAspect(width: number, height: number): string {
    if (width <= 0 || height <= 0) return "1:1";
    const actual = width / height;
    let best = "1:1";
    let bestDiff = Infinity;
    for (const name of SUPPORTED_ASPECTS) {
        const [aw, ah] = name.split(":").map(Number);
        if (!aw || !ah) continue;
        const diff = Math.abs(actual - aw / ah);
        if (diff < bestDiff) {
            bestDiff = diff;
            best = name;
        }
    }
    return best;
}

function buildMatrixTargets(): string[] {
    const out: string[] = [];
    for (const f of FACE_NAMES) {
        for (const e of EYES_STATES) {
            for (const m of MOUTH_SHAPES) {
                out.push(`${f}_${e}_${m}`);
            }
        }
    }
    return out;
}

function buildLayeredTargets(): string[] {
    const out: string[] = [];
    for (const s of EYES_STATES) out.push(`eyes_${s}`);
    for (const m of MOUTH_SHAPES) out.push(`mouth_${m}`);
    return out;
}

function getStageTargets(
    stageId: string,
    fallbackFiles: StageFile[],
): string[] {
    if (stageId === "01_face") return ["face"];
    if (stageId === "02_expressions") return EXPRESSION_NAMES;
    if (stageId === "03_matrix") return buildMatrixTargets();
    if (stageId === "03_layered") return buildLayeredTargets();
    // 04_trimmed: 中身は ③ の結果なので、 既存ファイルから割り出す。
    return fallbackFiles.map((f) => f.target);
}

// variant_key (= matrix では face、 layered では target) に対応する代表
// 画像 URL を求める。 visual rect editor の背景に使う。
function _trimSampleUrlForVariant(
    variantKey: string,
    mode: "matrix" | "layered",
    imageUrl: (stageId: string, filename: string) => string,
): string | null {
    if (mode === "matrix") {
        // matrix の variant = face name。 代表画像は ① or ② の表情画像。
        if (variantKey === "idle") {
            return imageUrl("01_face", "face.png");
        }
        return imageUrl("02_expressions", `${variantKey}.png`);
    }
    // layered の variant = target name (= face_<n> / eyes_<s> / mouth_<m>)。
    if (variantKey === "face_idle") {
        return imageUrl("01_face", "face.png");
    }
    if (variantKey.startsWith("face_")) {
        const expr = variantKey.slice("face_".length);
        return imageUrl("02_expressions", `${expr}.png`);
    }
    return imageUrl("03_layered", `${variantKey}.png`);
}

// ④ で trim 上書きを管理する単位 = variant_key の一覧。
function variantKeysForMode(mode: "matrix" | "layered"): string[] {
    if (mode === "matrix") {
        return [...FACE_NAMES];  // idle / happy / thinking / sad / surprised / embarrassed
    }
    return buildLayeredTargets();
}

// ----- Main modal -----

type TemplatesResponse = {
    common_prompt_hint?: string;
    [stageId: string]: Record<string, string> | string | undefined;
};

export default function AvatarPipelineModal({
    addonApiBase, personaId, personaName, setName,
    debugMode, onClose, onChanged,
}: AvatarPipelineModalProps) {
    const [info, setInfo] = useState<SetInfo | null>(null);
    const [templates, setTemplates] = useState<TemplatesResponse>({});
    const [currentStageIdx, setCurrentStageIdx] = useState(0);
    // 初回 info 取得後に「ファイル無し最初の stage」 で開く。
    // 既存セットが ④ まで進んでいたら ① から「次へ」 連打する手間を省く。
    // useRef で「初期化済みフラグ」 を持ち、 fetchInfo の再取得 (= 操作後の
    // 再 fetch) では currentStageIdx を上書きしない。
    const stageIdxInitialized = useRef(false);
    // ① の経路 (生成 / アップロード) を親で持つ。 CommonPromptSection の
    // 表示判定 (= アップロード派ならプロンプト欄 hide) に使う。
    const [faceMode, setFaceMode] = useState<"generate" | "upload">("generate");
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [lightbox, setLightbox] = useState<{
        url: string; alt: string;
    } | null>(null);
    // chain 進捗 (= per-target parallel chain の進捗、 ③ matrix の場合は
    // 最大 84 target、 layered は最大 6 target)。
    const [chainStatus, setChainStatus] = useState<{
        completedCount: number;
        failedCount: number;
        failedSamples: string[];  // 失敗 target の最初の数件 (= 表示用)
        total: number;
        running: boolean;
    } | null>(null);

    const baseUrl = `${addonApiBase}/avatar_sets/${encodeURIComponent(personaId)}/${encodeURIComponent(setName)}`;

    // テンプレート 1 回 fetch (= placeholder 表示用、 backend default と一致)。
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`${addonApiBase}/avatar_sets/templates`);
                if (!res.ok) return;
                const data = await res.json();
                if (!cancelled) setTemplates(data ?? {});
            } catch {
                // 取得失敗時は空 (= 既存挙動と同じ)
            }
        })();
        return () => { cancelled = true; };
    }, [addonApiBase]);

    // テンプレ取得ヘルパ (= 各段階の target ごとの default 文)。
    // backend DEFAULT_TEMPLATES の構造に合わせて lookup を分岐:
    //   01_face / 02_expressions: stage 直下 target lookup
    //   03_layered eyes_<s>: templates.eyes[s]
    //   03_layered mouth_<m>: templates.mouth[m]
    //   03_matrix <face>_<eyes>_<mouth>: face/eyes/mouth テンプレを連結
    const getTemplate = useCallback((
        stageId: string, target: string,
    ): string => {
        const lookup = (key: string, sub: string): string => {
            const stage = templates[key];
            if (stage && typeof stage === "object") {
                const v = (stage as Record<string, string>)[sub];
                if (typeof v === "string") return v;
            }
            return "";
        };
        if (stageId === "01_face" || stageId === "02_expressions") {
            return lookup(stageId, target);
        }
        if (stageId === "03_layered") {
            if (target.startsWith("eyes_")) {
                return lookup("eyes", target.slice("eyes_".length));
            }
            if (target.startsWith("mouth_")) {
                return lookup("mouth", target.slice("mouth_".length));
            }
            return "";
        }
        if (stageId === "03_matrix") {
            const parts = target.split("_");
            if (parts.length === 3) {
                const [face, eyes, mouth] = parts;
                const facePrompt = face === "idle"
                    ? lookup("01_face", "face")
                    : lookup("02_expressions", face);
                const eyesPrompt = lookup("eyes", eyes);
                const mouthPrompt = lookup("mouth", mouth);
                return [facePrompt, eyesPrompt, mouthPrompt]
                    .filter(Boolean)
                    .join(". ");
            }
        }
        return "";
    }, [templates]);

    const fetchInfo = useCallback(async () => {
        setError(null);
        try {
            const res = await fetch(baseUrl);
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail ?? `HTTP ${res.status}`);
            }
            const data: SetInfo = await res.json();
            setInfo(data);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    }, [baseUrl]);

    useEffect(() => { fetchInfo(); }, [fetchInfo]);

    const stages = useMemo(() => info?.wip_stages ?? [], [info]);

    // 起動時の stage 復元。 初回 info 取得後 1 回だけ「ファイル無し最初の
    // stage」 を選ぶ。 全 stage 完了済みなら最後の stage (④) を開く。
    useEffect(() => {
        if (stageIdxInitialized.current) return;
        if (stages.length === 0) return;
        let nextIdx = stages.findIndex((s) => s.files.length === 0);
        if (nextIdx < 0) nextIdx = stages.length - 1;
        setCurrentStageIdx(nextIdx);
        stageIdxInitialized.current = true;
    }, [stages]);
    const currentStage = stages[currentStageIdx];

    const callJson = async (
        method: string, path: string, body?: object,
    ): Promise<unknown> => {
        const res = await fetch(`${baseUrl}${path}`, {
            method,
            headers: body ? { "Content-Type": "application/json" } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
        if (!res.ok) {
            const errorBody = await res.json().catch(() => null);
            // error 型に status / detail を持たせる (= chain で 402 検出して
            // 即 abort するため、 まはー検証 2026-05-17 OpenAI billing 切れ)。
            const err = new Error(
                errorBody?.detail ?? `HTTP ${res.status}`,
            ) as Error & { status?: number; detail?: string };
            err.status = res.status;
            err.detail = errorBody?.detail;
            throw err;
        }
        return res.json();
    };

    const runApi = async <T,>(
        label: string, fn: () => Promise<T>,
    ): Promise<T | null> => {
        setBusy(label);
        setError(null);
        try {
            const result = await fn();
            await fetchInfo();
            onChanged?.();
            return result;
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            // 重要: error 時にも必ず fetchInfo を走らせる (= long-running の
            // matrix 生成で proxy timeout 等になっても backend は実行を続行
            // しているので、 部分成功 PNG は WIP に保存されている)。
            await fetchInfo().catch(() => undefined);
            onChanged?.();
            // 402 (= OpenAI billing 等の chain-abort 系) は re-throw して
            // chain ロジック側で即停止できるようにする (= 残り task で
            // 同じ error を繰り返すのを防ぐ)。 通常 status はそのまま
            // null return (= chain は次 target に進む)。
            const status = (e as { status?: number })?.status;
            if (status === 402) {
                throw e;
            }
            return null;
        } finally {
            setBusy(null);
        }
    };

    const updateMetadata = (updates: Partial<SetMetadata>) =>
        runApi("metadata 更新中", () =>
            callJson("PATCH", "/metadata", updates));

    const executeStage = (stageId: string, params?: object) =>
        runApi(`${STAGE_LABELS[stageId]} 実行中`, () =>
            callJson("POST", `/stages/${stageId}`, { params: params || {} }));

    const regenerateOne = (
        stageId: string, target: string, extraPrompt?: string,
    ) =>
        runApi(`${target} 再生成中`, () =>
            callJson("POST", `/stages/${stageId}/regenerate`, {
                target,
                params: extraPrompt ? { extra_prompt: extraPrompt } : undefined,
            }));

    const markCompleted = (stageId: string) =>
        runApi(`${STAGE_LABELS[stageId]} 完了マーク`, () =>
            callJson("POST", `/stages/${stageId}/complete`));

    const finalize = () => runApi("⑤ 確定化中", () =>
        callJson("POST", "/finalize"));

    // 手動 transfer は当面 UI から外す (= まはー検証 2026-05-17、
    // ⑤ finalize 時に Vessel 内ペルソナに自動転送される)。 backend
    // endpoint は残してあるので、 device プレビュー機能と組み合わせて
    // 再活用する想定。

    const importZip = (file: File, requireComplete: boolean) =>
        runApi(`zip 投入中 (${file.name})`, async () => {
            const form = new FormData();
            form.append("file", file);
            const url = `${baseUrl}/import_zip?require_complete=${requireComplete}`;
            const res = await fetch(url, { method: "POST", body: form });
            if (!res.ok) {
                const errorBody = await res.json().catch(() => null);
                throw new Error(errorBody?.detail ?? `HTTP ${res.status}`);
            }
            return res.json();
        });

    const uploadFace = (
        file: File,
        targetAspect: string,
        cropRect: { x: number; y: number; width: number; height: number } | null,
    ) =>
        runApi(`① 元顔アップロード (${file.name})`, async () => {
            const form = new FormData();
            form.append("file", file);
            form.append("target_aspect", targetAspect);
            if (cropRect) {
                form.append("crop_x", String(cropRect.x));
                form.append("crop_y", String(cropRect.y));
                form.append("crop_width", String(cropRect.width));
                form.append("crop_height", String(cropRect.height));
            }
            const url = `${baseUrl}/stages/01_face/upload`;
            const res = await fetch(url, { method: "POST", body: form });
            if (!res.ok) {
                const errorBody = await res.json().catch(() => null);
                throw new Error(errorBody?.detail ?? `HTTP ${res.status}`);
            }
            return res.json();
        });

    const uploadRefImage = (file: File) =>
        runApi(`参照画像 upload (${file.name})`, async () => {
            const form = new FormData();
            form.append("file", file);
            const url = `${baseUrl}/stages/01_face/ref_image`;
            const res = await fetch(url, { method: "POST", body: form });
            if (!res.ok) {
                const errorBody = await res.json().catch(() => null);
                throw new Error(errorBody?.detail ?? `HTTP ${res.status}`);
            }
            return res.json() as Promise<{ path: string; name: string }>;
        });

    const listRefImages = async () => {
        const url = `${baseUrl}/stages/01_face/ref_images`;
        const res = await fetch(url);
        if (!res.ok) return { refs: [] };
        return res.json() as Promise<{
            refs: { path: string; name: string }[];
        }>;
    };

    const deleteRefImage = (name: string) =>
        runApi(`参照画像削除 (${name})`, async () => {
            const url = `${baseUrl}/stages/01_face/ref_images/${encodeURIComponent(name)}`;
            const res = await fetch(url, { method: "DELETE" });
            if (!res.ok) {
                const errorBody = await res.json().catch(() => null);
                throw new Error(errorBody?.detail ?? `HTTP ${res.status}`);
            }
            return res.json();
        });

    const analyzeFaceImage = async (file: File) => {
        // persona/set 不要、 解析 endpoint。
        const form = new FormData();
        form.append("file", file);
        // baseUrl は /avatar_sets/{persona}/{set} なので、 analyze は別の path。
        const url = `${addonApiBase}/avatar_sets/stages/01_face/analyze`;
        const res = await fetch(url, { method: "POST", body: form });
        if (!res.ok) {
            const errorBody = await res.json().catch(() => null);
            throw new Error(errorBody?.detail ?? `HTTP ${res.status}`);
        }
        return res.json() as Promise<{
            width: number;
            height: number;
            suggested_aspect: string;
            supported_aspects: string[];
        }>;
    };

    const runFinalChain = async () => {
        // ④⑤ チェーン。 ⑥ 転送は ⑤ で Vessel 内なら自動実行
        // (= まはー検証 2026-05-17、 手動 transfer は撤去)。
        const trimStage = stages[stages.length - 1];
        if (!trimStage) return;
        const trimResult = await executeStage(trimStage.stage_id);
        if (!trimResult) return;
        await markCompleted(trimStage.stage_id);
        await finalize();
    };

    const goToStage = (idx: number) => {
        if (idx < 0 || idx >= stages.length) return;
        if (idx === 0) { setCurrentStageIdx(0); return; }
        const prev = stages[idx - 1];
        // 前 stage にファイルがあれば次へ進める。
        // 「完了マーク」 概念は廃止し、 ファイル有無を遷移ゲートにする。
        if (prev && prev.files.length > 0) {
            setCurrentStageIdx(idx);
        }
    };

    const imageUrl = (stageId: string, filename: string) => {
        const ts = info?.wip_metadata?.updated_at ?? "0";
        return `${baseUrl}/files/${stageId}/${filename}?t=${encodeURIComponent(ts)}`;
    };

    const openLightbox = (url: string, alt: string) =>
        setLightbox({ url, alt });

    if (!info) {
        return (
            <Overlay onClose={onClose}>
                <div style={styles.modal}>
                    {error ? (
                        <div style={styles.errorBox}>エラー: {error}</div>
                    ) : (
                        <div style={styles.loading}>読み込み中...</div>
                    )}
                    <button onClick={onClose} style={styles.btn}>閉じる</button>
                </div>
            </Overlay>
        );
    }

    return (
        <Overlay onClose={onClose}>
            <div style={styles.modal}>
                {/* ヘッダ */}
                <div style={styles.header}>
                    <div>
                        <div style={styles.title}>Avatar 制作</div>
                        <div style={styles.subtitle}>
                            {personaName} / {setName} (mode={info.wip_metadata?.mode})
                            {info.is_active && (
                                <span style={styles.activeBadge}>active</span>
                            )}
                        </div>
                    </div>
                    <button onClick={onClose} style={styles.closeBtn}>×</button>
                </div>

                {busy && <div style={styles.busyBox}>● {busy}</div>}
                {chainStatus && (
                    <div style={styles.chainBox}>
                        chain 進捗:
                        完了 {chainStatus.completedCount}/{chainStatus.total}
                        {chainStatus.running && <span> (実行中)</span>}
                        {chainStatus.failedCount > 0 && (
                            <span style={styles.chainFailed}>
                                {" "}失敗 {chainStatus.failedCount}
                                {chainStatus.failedSamples.length > 0 && (
                                    <span style={styles.subtle}>
                                        {" "}例: {
                                            chainStatus.failedSamples
                                                .slice(0, 3).join(", ")
                                        }
                                        {chainStatus.failedSamples.length > 3
                                            && " ..."}
                                    </span>
                                )}
                            </span>
                        )}
                    </div>
                )}
                {error && <div style={styles.errorBox}>エラー: {error}</div>}

                {/* 画像生成オプション: ① でアップロードを選んだ時はプロンプ
                    トやモデル設定が不要なので hide。 ②③④ に進んだ時 or
                    ① で生成を選んだ時のみ表示する (まはー指摘の「先に
                    生成/アップロード選んでから表示される流れ」)。 */}
                {(currentStageIdx > 0 || faceMode === "generate") && (
                    <CommonPromptSection
                        metadata={info.wip_metadata!}
                        debugMode={debugMode}
                        commonPromptHint={
                            typeof templates.common_prompt_hint === "string"
                                ? templates.common_prompt_hint
                                : ""
                        }
                        onUpdate={updateMetadata}
                    />
                )}

                {/* アニメーションプレビューは ③ stage に限定 + 制約プロンプト
                    の下に配置 (= StagePanel 内、 Stage3ConstraintEditor の
                    直下)。 ここではレンダリングしない。 */}

                {/* 段階バー: 「完了済み」 はファイル有無で判定。
                    backend の completed_stages フラグは ④ runFinalChain で
                    自動更新されるが、 ユーザー視点では「ファイルが生成され
                    たら完了」 が直感的なのでファイル数を真値とする。 */}
                <div style={styles.stageBar}>
                    {stages.map((s, i) => {
                        const isActive = i === currentStageIdx;
                        const hasFiles = s.files.length > 0;
                        return (
                            <button
                                key={s.stage_id}
                                onClick={() => goToStage(i)}
                                style={{
                                    ...styles.stageBtn,
                                    ...(isActive ? styles.stageBtnActive : {}),
                                    ...(hasFiles ? styles.stageBtnComplete : {}),
                                }}
                            >
                                {STAGE_LABELS[s.stage_id]}
                                {hasFiles && (
                                    <span style={styles.stageBtnCount}>
                                        {" "}({s.files.length})
                                    </span>
                                )}
                            </button>
                        );
                    })}
                </div>

                {/* 現在段階の本体 */}
                <div style={styles.stageBody}>
                    {currentStage && (
                        currentStage.stage_id === "01_face" ? (
                            <FaceStagePanel
                                stage={currentStage}
                                mode={faceMode}
                                onSetMode={setFaceMode}
                                metadata={info.wip_metadata!}
                                imageUrl={imageUrl}
                                onZoom={openLightbox}
                                debugMode={debugMode}
                                faceTemplate={getTemplate("01_face", "face")}
                                onExecute={(params) =>
                                    executeStage(
                                        currentStage.stage_id, params,
                                    )}
                                onUploadFace={uploadFace}
                                onAnalyzeImage={analyzeFaceImage}
                                onUploadRefImage={uploadRefImage}
                                onListRefImages={listRefImages}
                                onDeleteRefImage={deleteRefImage}
                                onMarkCompleted={() =>
                                    markCompleted(
                                        currentStage.stage_id,
                                    )}
                                onUpdateMetadata={updateMetadata}
                            />
                        ) : (
                            <StagePanel
                                stage={currentStage}
                                allStages={stages}
                                metadata={info.wip_metadata!}
                                imageUrl={imageUrl}
                                onZoom={openLightbox}
                                debugMode={debugMode}
                                getTemplate={getTemplate}
                                chainStatus={chainStatus}
                                setChainStatus={setChainStatus}
                                fetchInfo={fetchInfo}
                                onExecute={(params) =>
                                    executeStage(
                                        currentStage.stage_id, params,
                                    )}
                                onRegenerate={(target, extra) =>
                                    regenerateOne(
                                        currentStage.stage_id,
                                        target, extra,
                                    )}
                                onMarkCompleted={() =>
                                    markCompleted(currentStage.stage_id)}
                                onUpdateMetadata={updateMetadata}
                                onImportZip={
                                    currentStage.stage_id === "04_trimmed"
                                        ? importZip
                                        : undefined
                                }
                            />
                        )
                    )}
                </div>

                {/* ⑤ + 連続実行 (= ④の時に表示)。 ⑥ 転送は ⑤ で自動。 */}
                {currentStage?.stage_id === "04_trimmed" && (
                    <div style={styles.finalSection}>
                        <div style={styles.finalTitle}>
                            ⑤ 確定化:
                        </div>
                        <button
                            onClick={finalize}
                            disabled={!!busy}
                            style={styles.btnPrimary}
                        >
                            ⑤ 確定化 (avatar.bin 書き出し)
                        </button>
                        <button
                            onClick={runFinalChain}
                            disabled={!!busy}
                            style={styles.btnAccent}
                        >
                            ④⑤ 連続実行
                        </button>
                        <div style={styles.subtle}>
                            ※ ペルソナが Vessel Building 内なら ⑤ で
                            自動 device 転送、 外にいる場合は次の入室で
                            自動転送される。
                        </div>
                        {info.has_finalized && (
                            <div style={styles.subtle}>
                                確定品: {info.finalized_checksum?.slice(0, 24)}...
                            </div>
                        )}
                    </div>
                )}

                {/* Lightbox (= サムネイル拡大表示) */}
                {lightbox && (
                    <Lightbox
                        url={lightbox.url}
                        alt={lightbox.alt}
                        onClose={() => setLightbox(null)}
                    />
                )}

                {/* ナビ */}
                <div style={styles.navBar}>
                    <button
                        onClick={() => goToStage(currentStageIdx - 1)}
                        disabled={currentStageIdx === 0}
                        style={styles.btn}
                    >
                        ← 前へ
                    </button>
                    <button
                        onClick={() => goToStage(currentStageIdx + 1)}
                        disabled={currentStageIdx === stages.length - 1}
                        style={styles.btn}
                    >
                        次へ →
                    </button>
                </div>
            </div>
        </Overlay>
    );
}

// ----- アニメーションプレビュー (= 目パチ口パクレビュー) -----
//
// 表情 + 目 mode + 口 mode + 速度を切替えながら、 setInterval で
// 04_trimmed (= ④完了済み) または 03_matrix / 03_layered (= ③完了済み)
// の画像を時系列で差し替えてアニメーション再生。
//
// matrix mode: 1 img の src を `{face}_{eyes}_{mouth}.png` 形式で切替
// layered mode: face/eyes/mouth の 3 layer を CSS 重ねて、 eyes/mouth の
//   src だけ切替 (= firmware と同じ重ね方)。

type EyeMode = "blink" | "open" | "closed" | "half";
type MouthMode = "lipsync" | "closed" | "all_random";

interface AnimationFrame {
    eyes: string;
    mouth: string;
}

// 瞬きパターン (= 1 サイクル分の frame 列)
const BLINK_SEQUENCE: {
    eyes: "open" | "half" | "closed";
    durationMs: number;
}[] = [
    { eyes: "open", durationMs: 100 },
    { eyes: "half", durationMs: 60 },
    { eyes: "closed", durationMs: 80 },
    { eyes: "half", durationMs: 60 },
];

// 瞬きの間隔 (= open 状態の保持時間、 ランダム範囲)
const BLINK_INTERVAL_MIN_MS = 1500;
const BLINK_INTERVAL_MAX_MS = 4000;

// 口パクの 1 frame 保持時間 (= ランダム範囲)
const MOUTH_FRAME_MIN_MS = 80;
const MOUTH_FRAME_MAX_MS = 200;

const LIPSYNC_SHAPES = ["closed", "half", "open"];
const ALL_MOUTH_SHAPES = ["closed", "half", "open", "e", "u"];

function pickRandom<T>(arr: T[]): T {
    return arr[Math.floor(Math.random() * arr.length)];
}

function randomBetween(min: number, max: number): number {
    return Math.floor(Math.random() * (max - min)) + min;
}

function AnimationPreviewSection({
    metadata, stages, imageUrl,
}: {
    metadata: SetMetadata;
    stages: StageState[];
    imageUrl: (stageId: string, filename: string) => string;
}) {
    const mode = metadata.mode;
    const [face, setFace] = useState<string>("idle");
    const [eyeMode, setEyeMode] = useState<EyeMode>("blink");
    const [mouthMode, setMouthMode] = useState<MouthMode>("lipsync");
    const [speedMul, setSpeedMul] = useState<number>(1.0);
    const [playing, setPlaying] = useState<boolean>(false);
    const [current, setCurrent] = useState<AnimationFrame>({
        eyes: "open", mouth: "closed",
    });

    // どの段階から画像を取るか決定: 04_trimmed > 03_matrix/03_layered。
    const sourceStageId = useMemo(() => {
        const trimmed = stages.find((s) => s.stage_id === "04_trimmed");
        if (trimmed && trimmed.files.length > 0) return "04_trimmed";
        if (mode === "matrix") return "03_matrix";
        return "03_layered";
    }, [stages, mode]);

    const sourceStage = stages.find((s) => s.stage_id === sourceStageId);
    const availableTargets = useMemo(
        () => new Set(sourceStage?.files.map((f) => f.target) ?? []),
        [sourceStage],
    );

    // アニメーション state machine: 再生中は setTimeout で次 frame をスケジュール。
    useEffect(() => {
        if (!playing) return;
        let cancelled = false;
        let timerId: ReturnType<typeof setTimeout> | null = null;

        // 目: blink mode なら open 状態を待ってから BLINK_SEQUENCE を回す。
        //     固定 mode なら 1 回 set して終わり (= useEffect 経由で反映)。
        // 口: lipsync / all_random なら 1 frame ごとにランダム切替、
        //     closed 固定なら 1 回 set して終わり。

        let eyeIndex = -1;  // -1 = open 待機、 0..3 = BLINK_SEQUENCE index
        let nextOpenWaitMs = randomBetween(
            BLINK_INTERVAL_MIN_MS, BLINK_INTERVAL_MAX_MS,
        );
        let currentEyes = (
            eyeMode === "blink" ? "open" : eyeMode
        );
        let currentMouth = (
            mouthMode === "closed" ? "closed" : "closed"
        );

        const apply = () => {
            if (cancelled) return;
            setCurrent({ eyes: currentEyes, mouth: currentMouth });
        };

        const step = () => {
            if (cancelled) return;
            // 目の次状態決定
            if (eyeMode === "blink") {
                if (eyeIndex < 0) {
                    // open 中 → 次は BLINK_SEQUENCE[0]
                    currentEyes = "open";
                    eyeIndex = 0;
                    apply();
                    timerId = setTimeout(step, nextOpenWaitMs / speedMul);
                    return;
                }
                if (eyeIndex < BLINK_SEQUENCE.length) {
                    currentEyes = BLINK_SEQUENCE[eyeIndex].eyes;
                    const dur = BLINK_SEQUENCE[eyeIndex].durationMs;
                    eyeIndex++;
                    apply();
                    timerId = setTimeout(step, dur / speedMul);
                    return;
                }
                // sequence 終了 → open に戻して次の待機
                eyeIndex = -1;
                nextOpenWaitMs = randomBetween(
                    BLINK_INTERVAL_MIN_MS, BLINK_INTERVAL_MAX_MS,
                );
                currentEyes = "open";
                apply();
                timerId = setTimeout(step, nextOpenWaitMs / speedMul);
                return;
            }
            // 目固定 mode は state 既設、 timer なしで終わり
            // → 口 mode で動かす必要があれば下の口 timer に任せる。
            currentEyes = eyeMode;
            apply();
        };

        // 口は別 timer で回す (= 目とは独立周期)。
        const mouthStep = () => {
            if (cancelled) return;
            if (mouthMode === "closed") {
                currentMouth = "closed";
                apply();
                return;
            }
            const shapes = (
                mouthMode === "lipsync"
                    ? LIPSYNC_SHAPES : ALL_MOUTH_SHAPES
            );
            currentMouth = pickRandom(shapes);
            apply();
            timerId = setTimeout(
                mouthStep,
                randomBetween(MOUTH_FRAME_MIN_MS, MOUTH_FRAME_MAX_MS)
                / speedMul,
            );
        };

        // 初期 apply。 目固定 / 口固定なら 1 度切で終わり、
        // blink / lipsync は timer で継続。
        currentEyes = eyeMode === "blink" ? "open" : eyeMode;
        currentMouth = mouthMode === "closed" ? "closed" : "closed";
        apply();

        // 目 timer を起動 (blink mode のみ)
        const eyeTimerId = (
            eyeMode === "blink"
                ? setTimeout(
                    step, nextOpenWaitMs / speedMul,
                )
                : null
        );
        // 口 timer を起動 (動的 mode のみ)
        const mouthTimerId = (
            mouthMode !== "closed"
                ? setTimeout(
                    mouthStep,
                    randomBetween(MOUTH_FRAME_MIN_MS, MOUTH_FRAME_MAX_MS)
                    / speedMul,
                )
                : null
        );

        return () => {
            cancelled = true;
            if (timerId) clearTimeout(timerId);
            if (eyeTimerId) clearTimeout(eyeTimerId);
            if (mouthTimerId) clearTimeout(mouthTimerId);
        };
    }, [playing, face, eyeMode, mouthMode, speedMul]);

    // ファイル名解決 + 存在チェック。
    const matrixFile = `${face}_${current.eyes}_${current.mouth}.png`;
    const matrixExists = availableTargets.has(
        matrixFile.replace(/\.png$/, ""),
    );

    return (
        <details style={styles.section} open>
            <summary style={styles.sectionTitle}>
                アニメーションプレビュー
                <span style={styles.subtle}>
                    (source: {sourceStageId},
                    available: {availableTargets.size} frames)
                </span>
            </summary>
            <div style={styles.animPreviewLayout}>
                {/* 画像表示エリア */}
                <div style={styles.animStage}>
                    {mode === "matrix" ? (
                        matrixExists ? (
                            <img
                                src={imageUrl(sourceStageId, matrixFile)}
                                alt={matrixFile}
                                style={styles.animImage}
                            />
                        ) : (
                            <div style={styles.animMissing}>
                                該当 frame が無い: {matrixFile}
                            </div>
                        )
                    ) : (
                        <LayeredAnimStage
                            sourceStageId={sourceStageId}
                            face={face}
                            eyes={current.eyes}
                            mouth={current.mouth}
                            availableTargets={availableTargets}
                            imageUrl={imageUrl}
                        />
                    )}
                </div>

                {/* コントロール */}
                <div style={styles.animControls}>
                    <div style={styles.row}>
                        <label style={styles.label}>表情:</label>
                        <select
                            value={face}
                            onChange={(e) => setFace(e.target.value)}
                            style={styles.select}
                        >
                            {FACE_NAMES.map((n) => (
                                <option key={n} value={n}>{n}</option>
                            ))}
                        </select>
                    </div>
                    <div style={styles.row}>
                        <label style={styles.label}>目:</label>
                        <select
                            value={eyeMode}
                            onChange={(e) =>
                                setEyeMode(e.target.value as EyeMode)}
                            style={styles.select}
                        >
                            <option value="blink">瞬き ON</option>
                            <option value="open">常時 open</option>
                            <option value="half">常時 half</option>
                            <option value="closed">常時 closed</option>
                        </select>
                    </div>
                    <div style={styles.row}>
                        <label style={styles.label}>口:</label>
                        <select
                            value={mouthMode}
                            onChange={(e) =>
                                setMouthMode(e.target.value as MouthMode)}
                            style={styles.select}
                        >
                            <option value="lipsync">
                                口パク (open/half/closed)
                            </option>
                            <option value="closed">常時 closed</option>
                            <option value="all_random">
                                全ランダム (e/u 含む)
                            </option>
                        </select>
                    </div>
                    <div style={styles.row}>
                        <label style={styles.label}>速度:</label>
                        <input
                            type="range"
                            min={0.25}
                            max={3.0}
                            step={0.25}
                            value={speedMul}
                            onChange={(e) =>
                                setSpeedMul(parseFloat(e.target.value))}
                        />
                        <span style={styles.subtle}>{speedMul.toFixed(2)}x</span>
                    </div>
                    <div style={styles.row}>
                        <button
                            onClick={() => setPlaying(!playing)}
                            style={
                                playing ? styles.btnAccent : styles.btnPrimary
                            }
                        >
                            {playing ? "停止" : "▶ 再生"}
                        </button>
                        <span style={styles.subtle}>
                            current: eyes={current.eyes}, mouth={current.mouth}
                        </span>
                    </div>
                    {availableTargets.size === 0 && (
                        <div style={styles.subtle}>
                            (まず ③ で frame を生成して、 ④ で trim すると
                            再生できる)
                        </div>
                    )}
                </div>
            </div>
        </details>
    );
}

function LayeredAnimStage({
    sourceStageId, face, eyes, mouth, availableTargets, imageUrl,
}: {
    sourceStageId: string;
    face: string;
    eyes: string;
    mouth: string;
    availableTargets: Set<string>;
    imageUrl: (stageId: string, filename: string) => string;
}) {
    // layered mode の filename 規約 (= avatar_finalizer の _collect_trim_inputs
    // と avatar_generator の generate_stage_layered に合わせる):
    //   04_trimmed: face_<name> / eyes_<state> / mouth_<shape>
    //   03_layered: eyes_<state> / mouth_<shape> のみ、 face は 02/01 から
    //   ※ 04_trimmed があれば face_* も全部揃ってる前提
    const faceFile = `face_${face}.png`;
    const eyesFile = `eyes_${eyes}.png`;
    const mouthFile = `mouth_${mouth}.png`;
    const faceExists = availableTargets.has(`face_${face}`);
    const eyesExists = availableTargets.has(`eyes_${eyes}`);
    const mouthExists = availableTargets.has(`mouth_${mouth}`);

    if (sourceStageId !== "04_trimmed") {
        // 03_layered の段階では face 単体ファイルがない (= face は 02 / 01
        // 経由)。 簡易対応として「face は出ない、 eyes/mouth だけ」 を表示。
        return (
            <div style={styles.animLayerStack}>
                <div style={styles.animMissing}>
                    layered の face は ④ trim 後に揃う (= 現在 03_layered のみ)。
                    eyes/mouth プレビューのみ:
                </div>
                {eyesExists && (
                    <img
                        src={imageUrl(sourceStageId, eyesFile)}
                        alt={eyesFile}
                        style={styles.animImage}
                    />
                )}
            </div>
        );
    }

    return (
        <div style={styles.animLayerStack}>
            {faceExists && (
                <img
                    src={imageUrl(sourceStageId, faceFile)}
                    alt={faceFile}
                    style={styles.animLayerImg}
                />
            )}
            {eyesExists && (
                <img
                    src={imageUrl(sourceStageId, eyesFile)}
                    alt={eyesFile}
                    style={styles.animLayerImg}
                />
            )}
            {mouthExists && (
                <img
                    src={imageUrl(sourceStageId, mouthFile)}
                    alt={mouthFile}
                    style={styles.animLayerImg}
                />
            )}
            {!faceExists && (
                <div style={styles.animMissing}>face_{face} が無い</div>
            )}
        </div>
    );
}

// ----- Lightbox (= サムネイル拡大表示、 esc / 背景 / × で閉じる) -----

function Lightbox({
    url, alt, onClose,
}: { url: string; alt: string; onClose: () => void }) {
    useEffect(() => {
        const handler = (e: KeyboardEvent) => {
            if (e.key === "Escape") onClose();
        };
        window.addEventListener("keydown", handler);
        return () => window.removeEventListener("keydown", handler);
    }, [onClose]);

    return (
        <div
            style={styles.lightboxOverlay}
            onClick={onClose}
        >
            <div
                style={styles.lightboxInner}
                onClick={(e) => e.stopPropagation()}
            >
                <img src={url} alt={alt} style={styles.lightboxImage} />
                <div style={styles.lightboxFooter}>
                    <span style={styles.lightboxAlt}>{alt}</span>
                    <button onClick={onClose} style={styles.btn}>
                        閉じる (esc)
                    </button>
                </div>
            </div>
        </div>
    );
}

// ----- ClickableImage (= preview をクリックで Lightbox 開く) -----

function ClickableImage({
    src, alt, onZoom,
}: {
    src: string; alt: string; onZoom: (url: string, alt: string) => void;
}) {
    return (
        <img
            src={src}
            alt={alt}
            title="クリックで拡大"
            style={styles.preview}
            onClick={() => onZoom(src, alt)}
        />
    );
}

// ----- Overlay (= 共通の modal 背景) -----

function Overlay({
    children, onClose,
}: { children: React.ReactNode; onClose: () => void }) {
    // mousedown/mouseup で同 target でない時は誤発火扱いしない (= 既存
    // feedback_use_modal_overlay.md の罠を避ける、 ただし簡易版)。
    const [downOnOverlay, setDownOnOverlay] = useState(false);
    return (
        <div
            style={styles.overlay}
            onMouseDown={(e) => setDownOnOverlay(e.target === e.currentTarget)}
            onMouseUp={(e) => {
                if (downOnOverlay && e.target === e.currentTarget) {
                    onClose();
                }
                setDownOnOverlay(false);
            }}
        >
            {children}
        </div>
    );
}

// ----- 共通プロンプトセクション -----

function CommonPromptSection({
    metadata, debugMode, commonPromptHint, onUpdate,
}: {
    metadata: SetMetadata;
    debugMode: boolean;
    commonPromptHint: string;
    onUpdate: (updates: Partial<SetMetadata>) => Promise<unknown>;
}) {
    // 自動保存: textarea / number は onBlur、 select は onChange。
    // 「保存」 ボタンは廃止 (= 段階遷移で未保存変更が消える事故を防ぐ)。
    const [common, setCommon] = useState(metadata.common_prompt);
    const [parallelism, setParallelism] = useState(metadata.parallelism);

    useEffect(() => {
        setCommon(metadata.common_prompt);
        setParallelism(metadata.parallelism);
    }, [metadata.common_prompt, metadata.parallelism]);

    const saveCommon = () => {
        if (common !== metadata.common_prompt) {
            onUpdate({ common_prompt: common });
        }
    };
    const saveParallelism = () => {
        if (parallelism !== metadata.parallelism) {
            onUpdate({ parallelism });
        }
    };

    return (
        <details style={styles.section} open>
            <summary style={styles.sectionTitle}>
                画像生成オプション
                {debugMode && (
                    <span style={styles.debugTag}>+ Debug 設定</span>
                )}
                <span style={styles.autosaveTag}>自動保存</span>
            </summary>
            <div style={styles.label}>プロンプト</div>
            <textarea
                value={common}
                onChange={(e) => setCommon(e.target.value)}
                onBlur={saveCommon}
                placeholder={
                    commonPromptHint
                    || "ペルソナの外見を書く: 顔立ち、 髪色・髪型、 服装、 雰囲気など"
                }
                rows={3}
                style={styles.textarea}
            />
            <div style={styles.row}>
                <label style={styles.label}>モデル:</label>
                <select
                    value={metadata.image_model}
                    onChange={(e) =>
                        onUpdate({ image_model: e.target.value })}
                    style={styles.select}
                >
                    {IMAGE_MODELS.map((m) => (
                        <option key={m} value={m}>{m}</option>
                    ))}
                </select>
                <label style={styles.label}>並列度:</label>
                <input
                    type="number"
                    value={parallelism}
                    min={1}
                    max={20}
                    onChange={(e) =>
                        setParallelism(parseInt(e.target.value) || 5)}
                    onBlur={saveParallelism}
                    style={styles.numberInput}
                />
            </div>
            {debugMode && (
                <>
                    <div style={styles.row}>
                        <label style={styles.label}>quality:</label>
                        <select
                            value={metadata.image_quality}
                            onChange={(e) =>
                                onUpdate({ image_quality: e.target.value })}
                            style={styles.select}
                        >
                            {IMAGE_QUALITIES.map((q) => (
                                <option key={q} value={q}>{q}</option>
                            ))}
                        </select>
                        <label style={styles.label}>aspect_ratio:</label>
                        <select
                            value={metadata.aspect_ratio}
                            onChange={(e) =>
                                onUpdate({ aspect_ratio: e.target.value })}
                            style={styles.select}
                        >
                            {SUPPORTED_ASPECTS.map((a) => (
                                <option key={a} value={a}>{a}</option>
                            ))}
                        </select>
                        <span style={styles.subtle}>
                            ※ ① upload 時はその時点のアス比で上書きされる
                        </span>
                    </div>
                    <div style={styles.row}>
                        <label style={styles.checkboxLabel}>
                            <input
                                type="checkbox"
                                checked={metadata.apply_common_prompt_to_stage3}
                                onChange={(e) =>
                                    onUpdate({
                                        apply_common_prompt_to_stage3:
                                            e.target.checked,
                                    })}
                            />
                            ③ で共通プロンプトも適用する
                        </label>
                        <span style={styles.subtle}>
                            (default OFF: ③ は目・口差分が目的なので、 共通
                            プロンプトの外見記述が勝手に差分追加される事故を防ぐ)
                        </span>
                    </div>
                </>
            )}
        </details>
    );
}

// ----- 段階パネル -----

function StagePanel({
    stage, allStages, metadata, imageUrl, onZoom, debugMode, getTemplate,
    onExecute, onRegenerate, onMarkCompleted, onUpdateMetadata,
    onImportZip,
    chainStatus, setChainStatus, fetchInfo,
}: {
    stage: StageState;
    /** ③ matrix の grid 描画で「①② から base copy 予定」 を判定するため、
     *  全 stage の files を参照する必要がある。 */
    allStages: StageState[];
    metadata: SetMetadata;
    imageUrl: (stageId: string, filename: string) => string;
    onZoom: (url: string, alt: string) => void;
    debugMode: boolean;
    getTemplate: (stageId: string, target: string) => string;
    onExecute: (params?: object) => Promise<unknown>;
    onRegenerate: (target: string, extraPrompt?: string) => Promise<unknown>;
    onMarkCompleted: () => Promise<unknown>;
    onUpdateMetadata: (updates: Partial<SetMetadata>) => Promise<unknown>;
    onImportZip?: (file: File, requireComplete: boolean) => Promise<unknown>;
    chainStatus: {
        completedCount: number;
        failedCount: number;
        failedSamples: string[];
        total: number;
        running: boolean;
    } | null;
    setChainStatus: (s: typeof chainStatus) => void;
    fetchInfo: () => Promise<void>;
}) {
    const stageId = stage.stage_id;
    const stageExtras = metadata.extra_prompts[stageId] || {};
    const targets = getStageTargets(stageId, stage.files);
    // 単発実行用 state (② / ③ で使う)。
    const [singleExpr, setSingleExpr] = useState<string>("happy");
    const [singleFace, setSingleFace] = useState<string>("idle");
    const [singleEyes, setSingleEyes] = useState<string>("open");
    const [singleMouth, setSingleMouth] = useState<string>("closed");
    const [singleLayered, setSingleLayered] = useState<string>("eyes_open");

    // ③ per-target parallel chain (= まはー検証 2026-05-17)。
    // 既存「face 単位 chain」 は 1 リクエスト 5-7 分で Next.js proxy
    // timeout により 502。 これを「target 単位 (= 1 件) の単発 regenerate」
    // を parallelism 並列で fetch する形に置き換えると、 1 リクエスト 30 秒
    // で完結 → timeout 圏外。 84 件 ÷ 並列 5 = ~17 batch × 30 秒 ≈ 9 分。
    const runMatrixChain = async (
        skipExisting: boolean,
        faceFilter: string[] | null = null,
    ) => {
        // 対象 target を構築。
        let targets: string[];
        if (metadata.mode === "matrix") {
            const allFaces = faceFilter ?? FACE_NAMES;
            targets = [];
            for (const face of allFaces) {
                for (const eyes of EYES_STATES) {
                    for (const mouth of MOUTH_SHAPES) {
                        targets.push(`${face}_${eyes}_${mouth}`);
                    }
                }
            }
        } else {
            // layered は face filter なし (= eyes/mouth のみ)。
            targets = buildLayeredTargets();
        }
        if (skipExisting) {
            const existing = new Set(stage.files.map((f) => f.target));
            targets = targets.filter((t) => !existing.has(t));
        }
        if (targets.length === 0) {
            alert("対象 target なし (= 全部既存 or face 未選択)");
            return;
        }

        const total = targets.length;
        let completedCount = 0;
        let failedCount = 0;
        const failedSamples: string[] = [];
        const queue = [...targets];

        setChainStatus({
            completedCount: 0, failedCount: 0,
            failedSamples: [], total, running: true,
        });

        // chain 中の auto polling (= backend で生成中の preview を反映)。
        const pollId = window.setInterval(() => {
            fetchInfo().catch(() => undefined);
        }, 5000);

        const parallelism = Math.max(
            1, Math.min(metadata.parallelism || 5, 10),
        );

        // Chunk 並列実行 (= 旧 worker pool 実装で 1 件目失敗時に止まる
        // bug を回避、 まはー検証 2026-05-17)。 各 chunk で Promise.all
        // で N 件並列、 完了待ち → 次 chunk へ。 1 件失敗は他に影響なし。
        // 402 (= OpenAI billing 等) を検出したら chain 即停止 + dialog 表示
        // (= 残り task で同じ error 繰り返すのを防ぐ)。
        let billingAbortMessage: string | null = null;
        try {
            for (let i = 0; i < queue.length; i += parallelism) {
                if (billingAbortMessage) break;
                const batch = queue.slice(i, i + parallelism);
                const results = await Promise.all(
                    batch.map((target) =>
                        onRegenerate(target).then(
                            (r) => ({
                                target, ok: r !== null,
                                billing: false as boolean,
                                msg: "" as string,
                            }),
                            (err) => {
                                const status = (
                                    err as { status?: number }
                                )?.status;
                                const msg = (
                                    err instanceof Error
                                        ? err.message : String(err)
                                );
                                return {
                                    target, ok: false,
                                    billing: status === 402,
                                    msg,
                                };
                            },
                        ),
                    ),
                );
                for (const r of results) {
                    if (r.ok) {
                        completedCount++;
                    } else {
                        failedCount++;
                        if (failedSamples.length < 5) {
                            failedSamples.push(r.target);
                        }
                        if (r.billing && !billingAbortMessage) {
                            billingAbortMessage = r.msg;
                        }
                    }
                }
                setChainStatus({
                    completedCount, failedCount,
                    failedSamples: [...failedSamples],
                    total,
                    running:
                        !billingAbortMessage
                        && (i + parallelism) < queue.length,
                });
            }
        } finally {
            window.clearInterval(pollId);
            await fetchInfo().catch(() => undefined);
            setChainStatus({
                completedCount, failedCount,
                failedSamples, total, running: false,
            });
            // billing abort 時は dialog で明示 (= 「課金切れで止まった」
            // と確実に分かるように、 残り task をスキップした件数も伝える)。
            if (billingAbortMessage) {
                const remaining = total - completedCount - failedCount;
                alert(
                    `OpenAI billing 異常で chain を停止しました。\n\n`
                    + `${billingAbortMessage}\n\n`
                    + `処理状況: 完了 ${completedCount} / 失敗 `
                    + `${failedCount} / 残り未着手 ${remaining}`,
                );
            }
            setTimeout(() => setChainStatus(null), 15000);
        }
    };

    // 強制再生成の face 選択 picker state (matrix のみ使う)。
    const [forceFacePicker, setForceFacePicker] = useState<
        Set<string> | null
    >(null);

    const updateExtra = (target: string, extra: string) => {
        const newExtras = {
            ...metadata.extra_prompts,
            [stageId]: { ...stageExtras, [target]: extra },
        };
        onUpdateMetadata({ extra_prompts: newExtras });
    };

    return (
        <div>
            <div style={styles.stageHeader}>
                <div>
                    <div style={styles.stageHeaderTitle}>
                        {STAGE_LABELS[stageId]}
                        {stage.files.length > 0 && (
                            <span style={styles.completedBadge}>
                                ✓ {stage.files.length} 枚生成済み
                            </span>
                        )}
                    </div>
                    {STAGE_DESCRIPTIONS[stageId] && (
                        <div style={styles.stageDescription}>
                            {STAGE_DESCRIPTIONS[stageId]}
                        </div>
                    )}
                </div>
                <div style={styles.stageHeaderActions}>
                    <button
                        onClick={async () => {
                            // ③ は per-target parallel chain (= まはー検証
                            // 2026-05-17、 502 timeout 圏外、 進捗が target
                            // 単位で見える)。
                            if (
                                stageId === "03_matrix"
                                || stageId === "03_layered"
                            ) {
                                await runMatrixChain(true);
                                return;
                            }
                            await onExecute({ skip_existing: true });
                        }}
                        style={styles.btnPrimary}
                    >
                        {stage.files.length > 0
                            ? "未生成のものだけ生成"
                            : "全件生成"}
                    </button>
                    {/* 強制再生成: matrix は face checkbox で対象選択。
                        layered / ② は即実行 (= 全件上書き)。 */}
                    {stage.files.length > 0 && (
                        <button
                            onClick={() => {
                                if (stageId === "03_matrix") {
                                    setForceFacePicker(new Set(FACE_NAMES));
                                    return;
                                }
                                if (
                                    !confirm(
                                        "既存ファイルも上書きして全件再生成"
                                        + " (= API コスト発生)。 続行?",
                                    )
                                ) return;
                                if (stageId === "03_layered") {
                                    runMatrixChain(false);
                                    return;
                                }
                                onExecute().catch(() => undefined);
                            }}
                            style={styles.btn}
                        >
                            強制再生成
                        </button>
                    )}
                    {/* ② 単発テスト */}
                    {stageId === "02_expressions" && (
                        <>
                            <select
                                value={singleExpr}
                                onChange={(e) =>
                                    setSingleExpr(e.target.value)}
                                style={styles.select}
                            >
                                {EXPRESSION_NAMES.map((n) => (
                                    <option key={n} value={n}>{n}</option>
                                ))}
                            </select>
                            <button
                                onClick={() =>
                                    onExecute({ only_target: singleExpr })}
                                style={styles.btnAccent}
                            >
                                単発テスト
                            </button>
                        </>
                    )}
                    {/* ③ matrix 単発テスト (= 3 dropdown で組合せ構築) */}
                    {stageId === "03_matrix" && (
                        <>
                            <select
                                value={singleFace}
                                onChange={(e) =>
                                    setSingleFace(e.target.value)}
                                style={styles.select}
                            >
                                {FACE_NAMES.map((n) => (
                                    <option key={n} value={n}>{n}</option>
                                ))}
                            </select>
                            <select
                                value={singleEyes}
                                onChange={(e) =>
                                    setSingleEyes(e.target.value)}
                                style={styles.select}
                            >
                                {EYES_STATES.map((n) => (
                                    <option key={n} value={`${n}`}>
                                        eyes:{n}
                                    </option>
                                ))}
                            </select>
                            <select
                                value={singleMouth}
                                onChange={(e) =>
                                    setSingleMouth(e.target.value)}
                                style={styles.select}
                            >
                                {MOUTH_SHAPES.map((n) => (
                                    <option key={n} value={`${n}`}>
                                        mouth:{n}
                                    </option>
                                ))}
                            </select>
                            <button
                                onClick={() =>
                                    onExecute({
                                        only_target:
                                            `${singleFace}_${singleEyes}_${singleMouth}`,
                                    })}
                                style={styles.btnAccent}
                            >
                                単発テスト
                            </button>
                        </>
                    )}
                    {/* ③ layered 単発テスト */}
                    {stageId === "03_layered" && (
                        <>
                            <select
                                value={singleLayered}
                                onChange={(e) =>
                                    setSingleLayered(e.target.value)}
                                style={styles.select}
                            >
                                {buildLayeredTargets().map((n) => (
                                    <option key={n} value={n}>{n}</option>
                                ))}
                            </select>
                            <button
                                onClick={() =>
                                    onExecute({ only_target: singleLayered })}
                                style={styles.btnAccent}
                            >
                                単発テスト
                            </button>
                        </>
                    )}
                </div>
            </div>

            {debugMode && (
                <StageOverrideEditor
                    stageId={stageId}
                    metadata={metadata}
                    onUpdate={onUpdateMetadata}
                />
            )}

            {(stageId === "03_matrix" || stageId === "03_layered") && (
                <Stage3ConstraintEditor
                    metadata={metadata}
                    onUpdate={onUpdateMetadata}
                />
            )}

            {/* アニメプレビュー: ③ で目・口差分が 1 件以上生成された後に
                制約プロンプト直下で表示。 ここから下に各画像セルが並ぶ
                ので、 「生成済みの結果を全体動作で確認 → 個別セルで再
                生成」 という導線になる。 */}
            {(stageId === "03_matrix" || stageId === "03_layered")
                && stage.files.length > 0 && (
                <AnimationPreviewSection
                    metadata={metadata}
                    stages={allStages}
                    imageUrl={imageUrl}
                />
            )}

            {forceFacePicker && stageId === "03_matrix" && (
                <ForceFacePicker
                    selected={forceFacePicker}
                    onChange={setForceFacePicker}
                    onCancel={() => setForceFacePicker(null)}
                    onExecute={async () => {
                        const faces = Array.from(forceFacePicker);
                        setForceFacePicker(null);
                        if (faces.length === 0) return;
                        await runMatrixChain(false, faces);
                    }}
                />
            )}

            {stageId === "04_trimmed" && (
                <>
                    <TrimRectEditor
                        rect={metadata.trim_rect}
                        onChange={(rect) =>
                            onUpdateMetadata({ trim_rect: rect })}
                        sampleImageUrl={
                            imageUrl("01_face", "face.png")
                        }
                    />
                    <VariantOverridesSection
                        metadata={metadata}
                        imageUrl={imageUrl}
                        onUpdateMetadata={onUpdateMetadata}
                        onRunVariantTrim={(variantKey) => {
                            // matrix: face_filter で 14 セル一括 trim
                            // layered: only_target で 1 セル trim
                            const params = metadata.mode === "matrix"
                                ? { face_filter: variantKey }
                                : { only_target: variantKey };
                            return onExecute(params);
                        }}
                    />
                    {onImportZip && (
                        <ZipImportSection
                            mode={metadata.mode}
                            onImport={onImportZip}
                        />
                    )}
                </>
            )}

            {(() => {
            // ③ matrix は face ごとに 15 セル (3 目 × 5 口) でまとめて
            // 表示すると、 表情ごとの並びが追いやすい。 ① / ② / ③ layered
            // / ④ は flat grid のまま (= target 数が少ないか、 自然な並び
            // が既に存在する)。
            const renderCell = (target: string) => {
                const file = stage.files.find((f) => f.target === target);
                const extra = stageExtras[target] || "";
                // ③ matrix では eyes=open && mouth=closed の 6 セルは
                // ①② から自動 copy される (avatar_generator.py:737-754)。
                // ③ 実行前でも ①② に base 画像があれば「再利用予定」 と
                // 分かるよう、 そちらの画像をプレビュー表示する。
                const baseReuse = (() => {
                    if (file) return null;
                    if (stageId !== "03_matrix") return null;
                    const parts = target.split("_");
                    if (parts.length !== 3) return null;
                    const [face, eyes, mouth] = parts;
                    if (eyes !== "open" || mouth !== "closed") return null;
                    const baseStageId = face === "idle"
                        ? "01_face" : "02_expressions";
                    const baseTarget = face === "idle" ? "face" : face;
                    const baseStage = allStages.find(
                        (s) => s.stage_id === baseStageId,
                    );
                    if (!baseStage) return null;
                    const hasBase = baseStage.files.some(
                        (f) => f.target === baseTarget,
                    );
                    if (!hasBase) return null;
                    return {
                        url: imageUrl(baseStageId, `${baseTarget}.png`),
                        label: face === "idle" ? "① 元顔" : "② 表情差分",
                    };
                })();
                return (
                    <div key={target} style={styles.targetCell}>
                        {file ? (
                            <ClickableImage
                                src={imageUrl(stageId, `${target}.png`)}
                                alt={target}
                                onZoom={onZoom}
                            />
                        ) : baseReuse ? (
                            <>
                                <ClickableImage
                                    src={baseReuse.url}
                                    alt={target}
                                    onZoom={onZoom}
                                />
                                <div style={styles.baseReuseBadge}>
                                    {baseReuse.label} から自動コピー予定
                                </div>
                            </>
                        ) : (
                            <div style={styles.previewEmpty}>未生成</div>
                        )}
                        <div style={styles.targetLabel}>{target}</div>
                        {stageId !== "04_trimmed" && (
                            <>
                                <ExtraPromptInput
                                    value={extra}
                                    defaultTemplate={
                                        getTemplate(stageId, target)
                                    }
                                    onSave={(val) =>
                                        updateExtra(target, val)}
                                />
                                {file && (
                                    <AsyncButton
                                        onClick={() =>
                                            onRegenerate(
                                                target,
                                                extra || undefined,
                                            )}
                                        busyLabel="生成中..."
                                        style={styles.regenBtn}
                                        busyStyle={styles.regenBtnBusy}
                                    >
                                        再生成
                                    </AsyncButton>
                                )}
                            </>
                        )}
                    </div>
                );
            };

            if (stageId === "03_matrix") {
                return (
                    <div style={styles.matrixGroupContainer}>
                        {FACE_NAMES.map((face) => {
                            const faceTargets = targets.filter(
                                (t) => t.startsWith(`${face}_`),
                            );
                            return (
                                <div key={face} style={styles.matrixFaceGroup}>
                                    <div style={styles.matrixFaceHeader}>
                                        {face}
                                    </div>
                                    <div style={styles.targetGrid}>
                                        {faceTargets.map(renderCell)}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                );
            }
            return (
                <div style={styles.targetGrid}>
                    {targets.map(renderCell)}
                </div>
            );
            })()}
        </div>
    );
}

// ----- 非同期ボタン (= click 中は自分自身を「処理中...」 disabled に) -----
//
// modal 上部の busy 表示は grid を下方スクロール中だと視界に入らない。
// 各 target cell の「再生成」 / 「単発 trim」 等の単発ボタンは、 押された
// ボタン自体に loading を見せる方が UX 明快 (まはー指摘 2026-05-17)。

function AsyncButton({
    onClick, children, busyLabel = "処理中...",
    style, busyStyle, disabled,
}: {
    onClick: () => Promise<unknown>;
    children: React.ReactNode;
    busyLabel?: string;
    style?: React.CSSProperties;
    busyStyle?: React.CSSProperties;
    disabled?: boolean;
}) {
    const [loading, setLoading] = useState(false);
    const effective = (loading || disabled) ? busyStyle ?? style : style;
    return (
        <button
            disabled={loading || disabled}
            onClick={async () => {
                if (loading) return;
                setLoading(true);
                try {
                    await onClick();
                } finally {
                    setLoading(false);
                }
            }}
            style={effective}
        >
            {loading ? busyLabel : children}
        </button>
    );
}

// ----- per-target 追加自由文の入力 (= 自動保存: onBlur で 1 回 save) -----
//
// 値は metadata.extra_prompts[stageId][target] が source of truth。
// 入力中は local state、 onBlur で metadata と diff → 変化があれば save。
// placeholder に backend DEFAULT_TEMPLATES を表示 (= 空欄なら何が
// 渡されるか可視化)。

function ExtraPromptInput({
    value, defaultTemplate, onSave, rows = 2,
}: {
    value: string;
    defaultTemplate: string;
    onSave: (val: string) => void;
    rows?: number;
}) {
    const [local, setLocal] = useState(value);

    useEffect(() => { setLocal(value); }, [value]);

    const handleBlur = () => {
        if (local !== value) onSave(local);
    };

    const placeholder = defaultTemplate
        ? `(default: ${defaultTemplate})`
        : "追加自由文 (空なら何も追加されない)";

    return (
        <textarea
            value={local}
            onChange={(e) => setLocal(e.target.value)}
            onBlur={handleBlur}
            placeholder={placeholder}
            rows={rows}
            style={styles.targetExtraInput}
        />
    );
}

// ----- 段階別 quality / aspect override (Debug ON 時のみ) -----

function StageOverrideEditor({
    stageId, metadata, onUpdate,
}: {
    stageId: string;
    metadata: SetMetadata;
    onUpdate: (updates: Partial<SetMetadata>) => Promise<unknown>;
}) {
    const qOverride =
        metadata.stage_quality_overrides?.[stageId] ?? "";
    const aOverride =
        metadata.stage_aspect_overrides?.[stageId] ?? "";

    const setQOverride = (val: string) => {
        const next = { ...(metadata.stage_quality_overrides || {}) };
        if (val === "") delete next[stageId];
        else next[stageId] = val;
        onUpdate({ stage_quality_overrides: next });
    };

    const setAOverride = (val: string) => {
        const next = { ...(metadata.stage_aspect_overrides || {}) };
        if (val === "") delete next[stageId];
        else next[stageId] = val;
        onUpdate({ stage_aspect_overrides: next });
    };

    return (
        <div style={styles.debugBox}>
            <div style={styles.label}>
                Debug: この段階だけ quality / aspect 上書き
                (空欄 = セット default 使用)
            </div>
            <div style={styles.row}>
                <label style={styles.label}>quality:</label>
                <select
                    value={qOverride}
                    onChange={(e) => setQOverride(e.target.value)}
                    style={styles.select}
                >
                    <option value="">(default: {metadata.image_quality})</option>
                    {IMAGE_QUALITIES.map((q) => (
                        <option key={q} value={q}>{q}</option>
                    ))}
                </select>
                <label style={styles.label}>aspect:</label>
                <select
                    value={aOverride}
                    onChange={(e) => setAOverride(e.target.value)}
                    style={styles.select}
                >
                    <option value="">(default: {metadata.aspect_ratio})</option>
                    {SUPPORTED_ASPECTS.map((a) => (
                        <option key={a} value={a}>{a}</option>
                    ))}
                </select>
            </div>
        </div>
    );
}

// ----- ① 元顔生成 + アップロード経路 (Phase 4.5-d 追補) -----

function FaceStagePanel({
    stage, metadata, imageUrl, onZoom, debugMode, faceTemplate,
    onExecute, onUploadFace, onAnalyzeImage,
    onUploadRefImage, onListRefImages, onDeleteRefImage,
    onMarkCompleted, onUpdateMetadata, mode, onSetMode,
}: {
    stage: StageState;
    /** ① の経路 (生成 / アップロード) は親で state を持つ。
     *  CommonPromptSection の表示判定にも使うため。 */
    mode: "generate" | "upload";
    onSetMode: (mode: "generate" | "upload") => void;
    metadata: SetMetadata;
    imageUrl: (stageId: string, filename: string) => string;
    onZoom: (url: string, alt: string) => void;
    debugMode: boolean;
    faceTemplate: string;
    onExecute: (params?: object) => Promise<unknown>;
    onUploadFace: (
        file: File, targetAspect: string,
        cropRect: { x: number; y: number; width: number; height: number } | null,
    ) => Promise<unknown>;
    onAnalyzeImage: (file: File) => Promise<{
        width: number; height: number;
        suggested_aspect: string; supported_aspects: string[];
    }>;
    onUploadRefImage: (file: File) => Promise<unknown>;
    onListRefImages: () => Promise<{
        refs: { path: string; name: string }[];
    }>;
    onDeleteRefImage: (name: string) => Promise<unknown>;
    onMarkCompleted: () => Promise<unknown>;
    onUpdateMetadata: (updates: Partial<SetMetadata>) => Promise<unknown>;
}) {
    const setMode = onSetMode;
    const stageId = stage.stage_id;
    const stageExtras = metadata.extra_prompts[stageId] || {};
    const extra = stageExtras["face"] || "";

    const updateExtra = (val: string) => {
        const newExtras = {
            ...metadata.extra_prompts,
            [stageId]: { ...stageExtras, face: val },
        };
        onUpdateMetadata({ extra_prompts: newExtras });
    };

    return (
        <div>
            <div style={styles.stageHeader}>
                <div>
                    <div style={styles.stageHeaderTitle}>
                        {STAGE_LABELS[stageId]}
                        {stage.files.length > 0 && (
                            <span style={styles.completedBadge}>
                                ✓ 生成済み
                            </span>
                        )}
                    </div>
                    {STAGE_DESCRIPTIONS[stageId] && (
                        <div style={styles.stageDescription}>
                            {STAGE_DESCRIPTIONS[stageId]}
                        </div>
                    )}
                </div>
            </div>

            {/* 経路切替 (= 生成 / アップロード) */}
            <div style={styles.row}>
                <label style={styles.checkboxLabel}>
                    <input
                        type="radio"
                        checked={mode === "generate"}
                        onChange={() => setMode("generate")}
                    />
                    生成 (= プロンプト + 任意参照画像)
                </label>
                <label style={styles.checkboxLabel}>
                    <input
                        type="radio"
                        checked={mode === "upload"}
                        onChange={() => setMode("upload")}
                    />
                    アップロード (= 既存画像を直接配置)
                </label>
            </div>

            {debugMode && (
                <StageOverrideEditor
                    stageId={stageId}
                    metadata={metadata}
                    onUpdate={onUpdateMetadata}
                />
            )}

            {mode === "generate" ? (
                <FaceGenerateSubpanel
                    extra={extra}
                    defaultTemplate={faceTemplate}
                    onUpdateExtra={updateExtra}
                    onExecute={onExecute}
                    onUploadRefImage={onUploadRefImage}
                    onListRefImages={onListRefImages}
                    onDeleteRefImage={onDeleteRefImage}
                />
            ) : (
                <FaceUploadSubpanel
                    metadata={metadata}
                    onUploadFace={onUploadFace}
                    onAnalyzeImage={onAnalyzeImage}
                />
            )}

            {/* ①の生成結果プレビュー */}
            {stage.files.length > 0 && (
                <div style={styles.targetGrid}>
                    {stage.files.map((file) => (
                        <div key={file.target} style={styles.targetCell}>
                            <ClickableImage
                                src={imageUrl(stageId, `${file.target}.png`)}
                                alt={file.target}
                                onZoom={onZoom}
                            />
                            <div style={styles.targetLabel}>{file.target}</div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function FaceGenerateSubpanel({
    extra, defaultTemplate, onUpdateExtra, onExecute,
    onUploadRefImage, onListRefImages, onDeleteRefImage,
}: {
    extra: string;
    defaultTemplate: string;
    onUpdateExtra: (val: string) => void;
    onExecute: (params?: object) => Promise<unknown>;
    onUploadRefImage: (file: File) => Promise<unknown>;
    onListRefImages: () => Promise<{
        refs: { path: string; name: string }[];
    }>;
    onDeleteRefImage: (name: string) => Promise<unknown>;
}) {
    const [refs, setRefs] = useState<{ path: string; name: string }[]>([]);
    const [selectedRefs, setSelectedRefs] = useState<string[]>([]);
    // 自動保存: onBlur で 1 回 save (= updateExtra)。
    const [localExtra, setLocalExtra] = useState(extra);
    useEffect(() => { setLocalExtra(extra); }, [extra]);

    const refresh = useCallback(async () => {
        const data = await onListRefImages();
        setRefs(data.refs);
    }, [onListRefImages]);

    useEffect(() => { refresh(); }, [refresh]);

    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        await onUploadRefImage(file);
        await refresh();
        e.target.value = "";
    };

    const toggleRef = (path: string) => {
        setSelectedRefs((cur) =>
            cur.includes(path)
                ? cur.filter((p) => p !== path)
                : [...cur, path]
        );
    };

    return (
        <div style={styles.subpanel}>
            <div style={styles.label}>
                プロンプトに追加 (= 共通プロンプトに連結、 自動保存):
            </div>
            <textarea
                value={localExtra}
                onChange={(e) => setLocalExtra(e.target.value)}
                onBlur={() => {
                    if (localExtra !== extra) onUpdateExtra(localExtra);
                }}
                placeholder={
                    defaultTemplate
                        ? `(default: ${defaultTemplate})`
                        : "例: Neutral expression, calm idle face"
                }
                rows={2}
                style={styles.textarea}
            />

            <div style={styles.label}>
                参照画像 ({refs.length}枚保存中、 選択分が生成時に渡される):
            </div>
            <div style={styles.row}>
                <input
                    type="file"
                    accept="image/*"
                    onChange={handleUpload}
                    style={styles.fileInput}
                />
            </div>
            {refs.length > 0 && (
                <div style={styles.refGrid}>
                    {refs.map((r) => {
                        const selected = selectedRefs.includes(r.path);
                        return (
                            <div
                                key={r.name}
                                style={{
                                    ...styles.refCell,
                                    ...(selected ? styles.refCellSelected : {}),
                                }}
                            >
                                <div
                                    onClick={() => toggleRef(r.path)}
                                    style={{ cursor: "pointer" }}
                                >
                                    <div style={styles.refName}>{r.name}</div>
                                    <div style={styles.subtle}>
                                        {selected ? "選択中" : "未選択"}
                                    </div>
                                </div>
                                <button
                                    onClick={async () => {
                                        await onDeleteRefImage(r.name);
                                        await refresh();
                                        setSelectedRefs((cur) =>
                                            cur.filter((p) => p !== r.path)
                                        );
                                    }}
                                    style={styles.regenBtn}
                                >
                                    削除
                                </button>
                            </div>
                        );
                    })}
                </div>
            )}

            <div style={styles.row}>
                <button
                    onClick={() => onExecute({
                        ref_image_paths: selectedRefs.length > 0
                            ? selectedRefs : undefined,
                    })}
                    style={styles.btnPrimary}
                >
                    生成実行
                    {selectedRefs.length > 0
                        ? ` (参照 ${selectedRefs.length}枚)`
                        : ""}
                </button>
            </div>
        </div>
    );
}

function FaceUploadSubpanel({
    metadata, onUploadFace, onAnalyzeImage,
}: {
    metadata: SetMetadata;
    onUploadFace: (
        file: File, targetAspect: string,
        cropRect: { x: number; y: number; width: number; height: number } | null,
    ) => Promise<unknown>;
    onAnalyzeImage: (file: File) => Promise<{
        width: number; height: number;
        suggested_aspect: string; supported_aspects: string[];
    }>;
}) {
    const [file, setFile] = useState<File | null>(null);
    const [imgPreviewUrl, setImgPreviewUrl] = useState<string | null>(null);
    const [analysis, setAnalysis] = useState<{
        width: number; height: number;
        suggested_aspect: string; supported_aspects: string[];
    } | null>(null);
    const [targetAspect, setTargetAspect] = useState<string>(
        metadata.aspect_ratio,
    );
    const [crop, setCrop] = useState<TrimRect>(
        { x: 0, y: 0, width: 0, height: 0 },
    );
    const [lockAspect, setLockAspect] = useState<boolean>(true);
    const [analyzing, setAnalyzing] = useState(false);

    // file 選択中はプレビュー URL を生成、 解除時に revoke。
    useEffect(() => {
        if (!file) {
            setImgPreviewUrl(null);
            return;
        }
        const url = URL.createObjectURL(file);
        setImgPreviewUrl(url);
        return () => URL.revokeObjectURL(url);
    }, [file]);

    // セレクト変更 → crop 枠をそのアス比の中央矩形に refit。
    // (旧実装は useEffect [analysis, targetAspect] で refit していたが、
    // crop → セレクト同期 (updateCrop) と相互発火してユーザーのドラッグを
    // 巻き戻すため、 セレクト onChange / 解析完了時の明示呼び出しに変更。)
    const applyAspect = (
        aspect: string,
        a = analysis,
    ) => {
        setTargetAspect(aspect);
        if (a) {
            setCrop(fitRectToAspect(a.width, a.height, aspect));
        }
    };

    // crop 矩形が single source of truth: 変更されるたびに実アス比へ
    // セレクト表示を同期する (= 2026-06-10 の「① で 4:3 にトリミング
    // したのに ②③ が 1:1 生成」 事故の修正。 旧実装はセレクト値を
    // そのまま送信していて、 crop 枠との乖離に気づけなかった)。
    const updateCrop = (next: TrimRect) => {
        setCrop(next);
        setTargetAspect(closestSupportedAspect(next.width, next.height));
    };

    const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const f = e.target.files?.[0];
        if (!f) {
            setFile(null);
            setAnalysis(null);
            return;
        }
        setFile(f);
        setAnalyzing(true);
        try {
            const result = await onAnalyzeImage(f);
            setAnalysis(result);
            applyAspect(result.suggested_aspect, result);
        } catch {
            setAnalysis(null);
        } finally {
            setAnalyzing(false);
        }
    };

    const submit = async () => {
        if (!file) return;
        // 送信アス比は最終 crop 矩形から導出 (= セレクト表示は同期済みの
        // はずだが、 真実は常に crop 側)。 backend 側でも同じ導出をして
        // metadata に保存する 2 重ガード。
        await onUploadFace(
            file,
            closestSupportedAspect(crop.width, crop.height),
            crop,
        );
    };

    const saveIfDirty = () => {/* state 単体なので何もしない (auto sync) */};

    return (
        <div style={styles.subpanel}>
            <div style={styles.label}>
                既存の標準顔画像をそのまま配置 (生成スキップ)。 ②③ も同じ
                アス比で生成されるので、 ここで決めるトリミング範囲が以降
                全画像のキャンバスサイズを決める。
            </div>
            <div style={styles.row}>
                <input
                    type="file"
                    accept="image/*"
                    onChange={handleFile}
                    style={styles.fileInput}
                />
            </div>
            {analyzing && <div style={styles.subtle}>解析中...</div>}
            {analysis && imgPreviewUrl && (
                <>
                    <div style={styles.subtle}>
                        サイズ: {analysis.width}×{analysis.height}、
                        推奨アス比: {analysis.suggested_aspect}
                    </div>
                    <div style={styles.row}>
                        <label style={styles.label}>アス比:</label>
                        <select
                            value={targetAspect}
                            onChange={(e) => applyAspect(e.target.value)}
                            style={styles.select}
                        >
                            {analysis.supported_aspects.map((a) => (
                                <option key={a} value={a}>{a}</option>
                            ))}
                        </select>
                        <span style={styles.subtle}>
                            ②③ は {targetAspect} で生成されます (= crop
                            枠の実アス比に自動追従)
                        </span>
                    </div>
                    <div style={styles.label}>
                        クロップ範囲 (ドラッグで調整可能、 初期値は中央の
                        {" "}{targetAspect} 矩形):
                    </div>
                    <TrimRectVisualEditor
                        imageSrc={imgPreviewUrl}
                        rect={crop}
                        onChange={updateCrop}
                        lockAspect={lockAspect}
                        onLockAspectChange={setLockAspect}
                        lockAspectRatio={targetAspect}
                    />
                    <div style={styles.row}>
                        <label style={styles.label}>x:</label>
                        <input
                            type="number"
                            value={crop.x}
                            style={styles.numberInputSmall}
                            onChange={(e) =>
                                updateCrop({
                                    ...crop,
                                    x: parseInt(e.target.value) || 0,
                                })}
                            onBlur={saveIfDirty}
                        />
                        <label style={styles.label}>y:</label>
                        <input
                            type="number"
                            value={crop.y}
                            style={styles.numberInputSmall}
                            onChange={(e) =>
                                updateCrop({
                                    ...crop,
                                    y: parseInt(e.target.value) || 0,
                                })}
                            onBlur={saveIfDirty}
                        />
                        <label style={styles.label}>w:</label>
                        <input
                            type="number"
                            value={crop.width}
                            style={styles.numberInputSmall}
                            onChange={(e) =>
                                updateCrop({
                                    ...crop,
                                    width: parseInt(e.target.value) || 0,
                                })}
                            onBlur={saveIfDirty}
                        />
                        <label style={styles.label}>h:</label>
                        <input
                            type="number"
                            value={crop.height}
                            style={styles.numberInputSmall}
                            onChange={(e) =>
                                updateCrop({
                                    ...crop,
                                    height: parseInt(e.target.value) || 0,
                                })}
                            onBlur={saveIfDirty}
                        />
                    </div>
                    <div style={styles.row}>
                        <button
                            onClick={submit}
                            disabled={!file}
                            style={styles.btnPrimary}
                        >
                            アップロード + 配置
                        </button>
                    </div>
                </>
            )}
        </div>
    );
}

// ----- ③ 強制再生成の face checkbox picker -----
//
// 「強制再生成」 押下時に face を選んで対象を絞れる (= 既に確定した face
// (例: idle) を除外して API コスト節約)。 全 ON が default、 全 OFF で
// キャンセル相当。

function ForceFacePicker({
    selected, onChange, onCancel, onExecute,
}: {
    selected: Set<string>;
    onChange: (next: Set<string>) => void;
    onCancel: () => void;
    onExecute: () => void;
}) {
    const toggle = (face: string) => {
        const next = new Set(selected);
        if (next.has(face)) next.delete(face);
        else next.add(face);
        onChange(next);
    };
    const setAll = (on: boolean) => {
        onChange(on ? new Set(FACE_NAMES) : new Set());
    };
    // 14 通り × 選択 face = 想定生成数 (= base copy は除く、 eyes=open &
    // mouth=closed セル 6 件相当を引いた数)。
    const estimatedCount = selected.size * 14;

    return (
        <div style={styles.debugBox}>
            <div style={styles.label}>
                強制再生成する face を選択 (= 既存上書き、 API コスト発生):
            </div>
            <div style={styles.row}>
                {FACE_NAMES.map((face) => (
                    <label key={face} style={styles.checkboxLabel}>
                        <input
                            type="checkbox"
                            checked={selected.has(face)}
                            onChange={() => toggle(face)}
                        />
                        {face}
                    </label>
                ))}
            </div>
            <div style={styles.row}>
                <button onClick={() => setAll(true)} style={styles.btn}>
                    全 ON
                </button>
                <button onClick={() => setAll(false)} style={styles.btn}>
                    全 OFF
                </button>
                <span style={styles.subtle}>
                    生成見込: {estimatedCount} 件 (= 約 ${
                        (estimatedCount * 0.02).toFixed(2)
                    } / GPT-Image-2 low)
                </span>
            </div>
            <div style={styles.row}>
                <button
                    onClick={onExecute}
                    disabled={selected.size === 0}
                    style={
                        selected.size === 0
                            ? styles.btnDisabled : styles.btnAccent
                    }
                >
                    実行
                </button>
                <button onClick={onCancel} style={styles.btn}>
                    キャンセル
                </button>
            </div>
        </div>
    );
}

// ----- ③ constraint プロンプト編集 (= ポーズ維持文の上書き) -----
//
// default 文は backend DEFAULT_TEMPLATES["03_constraint"]["all"] (= 「参照
// 画像をもとに、 瞬き・口パクアニメーション...」)。 metadata.extra_prompts
// ["03_constraint"]["all"] で上書き可能。 自動保存 onBlur。

const STAGE3_DEFAULT_CONSTRAINT = (
    "参照画像をもとに、 瞬き・口パクアニメーションをさせるための"
    + "差分画像制作である。 構図・ポーズ・表す感情・服装などは"
    + "一切変えないこと。 さらに、 目と口のうち以下に指定がある側"
    + "のみを指定に合わせて変更し、 指定がない側 (目または口) は"
    + "参照画像から完全にそのままにすること。"
);

function Stage3ConstraintEditor({
    metadata, onUpdate,
}: {
    metadata: SetMetadata;
    onUpdate: (updates: Partial<SetMetadata>) => Promise<unknown>;
}) {
    const stored =
        metadata.extra_prompts?.["03_constraint"]?.["all"];
    const initial = typeof stored === "string"
        ? stored : STAGE3_DEFAULT_CONSTRAINT;
    const [local, setLocal] = useState(initial);

    useEffect(() => {
        setLocal(typeof stored === "string"
            ? stored : STAGE3_DEFAULT_CONSTRAINT);
    }, [stored]);

    const save = () => {
        const next = {
            ...(metadata.extra_prompts || {}),
            "03_constraint": { all: local },
        };
        onUpdate({ extra_prompts: next });
    };

    const resetToDefault = () => {
        // metadata.extra_prompts["03_constraint"] を削除 → backend が default
        // テンプレを使う形に戻す。
        const next = { ...(metadata.extra_prompts || {}) };
        delete next["03_constraint"];
        onUpdate({ extra_prompts: next });
    };
    const isOverridden = typeof stored === "string"
        && stored !== STAGE3_DEFAULT_CONSTRAINT;

    return (
        <details style={styles.section}>
            <summary style={styles.sectionTitle}>
                ③ 制約プロンプト (各画像生成の前に必ず付加)
                <span style={styles.autosaveTag}>自動保存</span>
                {isOverridden && (
                    <span style={styles.debugTag}>カスタム文使用中</span>
                )}
            </summary>
            <textarea
                value={local}
                onChange={(e) => setLocal(e.target.value)}
                onBlur={() => {
                    if (local !== (stored ?? STAGE3_DEFAULT_CONSTRAINT)) {
                        save();
                    }
                }}
                rows={3}
                style={styles.textarea}
            />
            <div style={styles.row}>
                {isOverridden && (
                    <button
                        onClick={resetToDefault}
                        style={styles.btn}
                    >
                        default 文に戻す
                    </button>
                )}
                <span style={styles.subtle}>
                    ポーズ・構図・表す感情の維持を AI に指示する文
                    (= まはー検証 2026-05-17)。 ③ 段階の全生成プロンプトの
                    先頭に追加される。 ② の長文表情テンプレは ③ には持ち込ま
                    ない (= 「sad expression」 など端的に明示のみ)。
                </span>
            </div>
        </details>
    );
}

// ----- ④ Variant overrides section (= まはー検証 2026-05-17) -----
//
// matrix: 6 face row (= 各 face の 15 セルは同 rect で十分)
// layered: 14 target row (= face / eyes / mouth 別々の rect)
//
// 各 row は折り畳み式。 「override 既存 」 なら自動展開、 そうでなければ
// 「rect 上書き」 ボタン押下で展開。 各 row に visual editor + 「単発 trim」
// (= matrix なら 14 セル一括、 layered なら 1 セル) + 「上書き削除」。

function VariantOverridesSection({
    metadata, imageUrl, onUpdateMetadata, onRunVariantTrim,
}: {
    metadata: SetMetadata;
    imageUrl: (stageId: string, filename: string) => string;
    onUpdateMetadata: (updates: Partial<SetMetadata>) => Promise<unknown>;
    onRunVariantTrim: (variantKey: string) => Promise<unknown>;
}) {
    const mode = metadata.mode;
    const variantKeys = variantKeysForMode(mode);
    const overrides = metadata.trim_rect_overrides || {};

    const handleSaveOverride = (
        variantKey: string,
        rect: { x: number; y: number; width: number; height: number } | null,
    ) => {
        const next = { ...overrides };
        if (rect) next[variantKey] = rect;
        else delete next[variantKey];
        return onUpdateMetadata({ trim_rect_overrides: next });
    };

    return (
        <details style={styles.section} open>
            <summary style={styles.sectionTitle}>
                Variant 単位 rect 上書き
                <span style={styles.subtle}>
                    {" "}({mode === "matrix"
                        ? "face 単位、 同 face の 15 セルは同 rect"
                        : "target 単位、 14 個別"})
                </span>
            </summary>
            <div style={styles.variantList}>
                {variantKeys.map((variantKey) => (
                    <VariantTrimRow
                        key={variantKey}
                        variantKey={variantKey}
                        defaultRect={metadata.trim_rect}
                        overrides={overrides}
                        sampleImageUrl={
                            _trimSampleUrlForVariant(
                                variantKey, mode, imageUrl,
                            )
                        }
                        onSaveOverride={(rect) =>
                            handleSaveOverride(variantKey, rect)}
                        onRunVariantTrim={() =>
                            onRunVariantTrim(variantKey)}
                        runLabel={
                            mode === "matrix"
                                ? `${variantKey} の 14-15 セルを trim`
                                : `${variantKey} を trim`
                        }
                    />
                ))}
            </div>
        </details>
    );
}

function VariantTrimRow({
    variantKey, defaultRect, overrides, sampleImageUrl,
    onSaveOverride, onRunVariantTrim, runLabel,
}: {
    variantKey: string;
    defaultRect: TrimRect | null;
    overrides: Record<string, TrimRect>;
    sampleImageUrl: string | null;
    onSaveOverride: (rect: TrimRect | null) => Promise<unknown>;
    onRunVariantTrim: () => Promise<unknown>;
    runLabel: string;
}) {
    const existing = overrides[variantKey];
    const [expanded, setExpanded] = useState<boolean>(!!existing);
    const init = existing ?? defaultRect ?? {
        x: 0, y: 0, width: 1024, height: 768,
    };
    const [x, setX] = useState(init.x);
    const [y, setY] = useState(init.y);
    const [w, setW] = useState(init.width);
    const [h, setH] = useState(init.height);
    const [lockAspect, setLockAspect] = useState<boolean>(true);
    // 編集対象のサンプル画像の natural size (= 数値 input 経由の保存にも
    // ref_width/ref_height を付与するため visual editor から受け取る)。
    const [refDims, setRefDims] = useState<{ w: number; h: number } | null>(
        null,
    );

    useEffect(() => {
        const e = overrides?.[variantKey];
        const src = e ?? defaultRect;
        if (src) {
            setX(src.x); setY(src.y);
            setW(src.width); setH(src.height);
        }
    }, [
        overrides, variantKey,
        defaultRect?.x, defaultRect?.y,
        defaultRect?.width, defaultRect?.height,
    ]);

    const saveIfDirty = () => {
        const dirty = !existing
            || existing.x !== x || existing.y !== y
            || existing.width !== w || existing.height !== h;
        if (dirty) {
            const rect: TrimRect = { x, y, width: w, height: h };
            if (refDims) {
                rect.ref_width = refDims.w;
                rect.ref_height = refDims.h;
            }
            onSaveOverride(rect);
        }
    };

    if (!expanded) {
        return (
            <div style={styles.variantRowCollapsed}>
                <span style={styles.variantLabel}>{variantKey}</span>
                <span style={styles.subtle}>
                    {existing ? "(上書き設定済み)" : "(default 使用)"}
                </span>
                <button
                    onClick={() => setExpanded(true)}
                    style={styles.btn}
                >
                    {existing ? "編集" : "rect 上書き"}
                </button>
                <AsyncButton
                    onClick={onRunVariantTrim}
                    busyLabel="trim 中..."
                    style={styles.btnAccent}
                    busyStyle={styles.btnDisabled}
                >
                    単発 trim
                </AsyncButton>
            </div>
        );
    }

    const currentRect = { x, y, width: w, height: h };
    const onVisualChange = (next: TrimRect) => {
        setX(next.x); setY(next.y);
        setW(next.width); setH(next.height);
        const stored = existing ?? defaultRect;
        const dirty = !stored
            || stored.x !== next.x || stored.y !== next.y
            || stored.width !== next.width || stored.height !== next.height;
        if (dirty) onSaveOverride(next);
    };

    return (
        <div style={styles.variantRowExpanded}>
            <div style={styles.row}>
                <span style={styles.variantLabel}>{variantKey}</span>
                <span style={styles.subtle}>
                    ({runLabel})
                </span>
            </div>
            <TrimRectVisualEditor
                imageSrc={sampleImageUrl}
                rect={currentRect}
                onChange={onVisualChange}
                lockAspect={lockAspect}
                onLockAspectChange={setLockAspect}
                onImageSize={(iw, ih) => setRefDims({ w: iw, h: ih })}
            />
            <div style={styles.row}>
                <label style={styles.label}>x:</label>
                <input
                    type="number" value={x} style={styles.numberInputSmall}
                    onChange={(e) => setX(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label style={styles.label}>y:</label>
                <input
                    type="number" value={y} style={styles.numberInputSmall}
                    onChange={(e) => setY(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label style={styles.label}>w:</label>
                <input
                    type="number" value={w} style={styles.numberInputSmall}
                    onChange={(e) => setW(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label style={styles.label}>h:</label>
                <input
                    type="number" value={h} style={styles.numberInputSmall}
                    onChange={(e) => setH(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
            </div>
            <div style={styles.row}>
                <AsyncButton
                    onClick={onRunVariantTrim}
                    busyLabel="trim 中..."
                    style={styles.btnAccent}
                    busyStyle={styles.btnDisabled}
                >
                    {runLabel}
                </AsyncButton>
                {existing && (
                    <button
                        onClick={() => onSaveOverride(null)}
                        style={styles.btn}
                    >
                        上書き削除
                    </button>
                )}
                <button
                    onClick={() => setExpanded(false)}
                    style={styles.btn}
                >
                    閉じる
                </button>
            </div>
        </div>
    );
}

// (旧 PerTargetTrimEditor は per-cell 編集だったが、 まはー検証で「同 face
// 15 セルは同 rect で十分」 と判明したため VariantOverridesSection /
// VariantTrimRow に置き換え。 2026-05-17 で dead code 削除)
//
// まはー指摘: 表情ごとに顔の中心位置が変わるので、 一律 trim_rect だと
// 揃わない。 各 target で「上書き rect」 を持てるようにする。
//
// 折り畳み default: override 既存なら開く / なければ閉じる。 「単発 trim」
// で該当 target だけ trim 再実行 (= 全件再実行を避けて速い iteration)。

// ----- ④ 視覚的 rect editor (= drag/resize overlay) -----
//
// まはー指摘 2026-05-17: 数値 4 input だけで 90 枚にトリム範囲を当てるのは
// 無理。 画像 + rect overlay + 8 ハンドル drag/resize + 4:3 アス比固定 toggle
// で視覚的に編集できるようにする。 数値 input は維持して微調整用に併存。

type TrimRect = {
    x: number; y: number; width: number; height: number;
    // 編集時に表示していた画像の natural size。 ④ で適用先画像のサイズが
    // ref と違う時、 backend (_trim_one) が rect を比例スケールして適用する
    // (= ① 手動アップロード由来 face.png と ②③ 生成画像のサイズ差対策)。
    ref_width?: number;
    ref_height?: number;
};

type DragMode =
    | "move"
    | "n" | "s" | "e" | "w"
    | "ne" | "nw" | "se" | "sw"
    | null;

function TrimRectVisualEditor({
    imageSrc, rect, onChange, lockAspect, onLockAspectChange,
    lockAspectRatio = "4:3", onImageSize,
}: {
    imageSrc: string | null;
    rect: TrimRect | null;
    onChange: (next: TrimRect) => void;
    lockAspect: boolean;
    onLockAspectChange: (locked: boolean) => void;
    /** lockAspect ON 時に固定するアス比。 ④ は ⑤ の 160×120 出力に合わせ
     *  default "4:3"、 ① アップロードでは選択中の target アス比を渡す。 */
    lockAspectRatio?: string;
    /** 画像 load 時に natural size を親へ通知 (= 数値 input 経由の保存にも
     *  ref_width/ref_height を付与するため)。 */
    onImageSize?: (w: number, h: number) => void;
}) {
    const containerRef = React.useRef<HTMLDivElement>(null);
    const imgRef = React.useRef<HTMLImageElement>(null);
    const [imgNatural, setImgNatural] = useState<{
        w: number; h: number;
    } | null>(null);
    const [drag, setDrag] = useState<{
        mode: DragMode;
        startClientX: number;
        startClientY: number;
        startRect: TrimRect;
    } | null>(null);
    // drag 中の仮 rect (= mousemove 60Hz で更新、 onChange は呼ばない)。
    // mouseup で 1 回だけ commit する (= まはー検証 2026-05-17、
    // 60Hz で onChange → PATCH /metadata を叩いてて backend / React
    // 過負荷で白画面化した bug の修正)。
    const [pendingRect, setPendingRect] = useState<TrimRect | null>(null);
    const displayRect = pendingRect ?? rect;

    // lockAspect ON 時に固定する比率 (= lockAspectRatio prop から導出)。
    const lockRatio = (() => {
        const [aw, ah] = lockAspectRatio.split(":").map(Number);
        return aw && ah ? aw / ah : 4 / 3;
    })();

    // 画像の natural size を取って、 「初回 rect 未設定なら画像全体」 を提案。
    // さらに lockAspect ON で rect が画像全体と等しい (= 未補正の初期値)
    // 場合、 中央 lockAspectRatio 矩形に自動補正する。 まはー指摘「最初の
    // 画像がアス比違いの時、 lockAspect ON でも初期 rect が元画像比率」 の解消。
    const handleImgLoad = () => {
        if (imgRef.current) {
            const w = imgRef.current.naturalWidth;
            const h = imgRef.current.naturalHeight;
            setImgNatural({ w, h });
            onImageSize?.(w, h);
            if (!rect || rect.width === 0 || rect.height === 0) {
                if (lockAspect) {
                    onChange({
                        ...fitRectToAspect(w, h, lockAspectRatio),
                        ref_width: w, ref_height: h,
                    });
                } else {
                    onChange({
                        x: 0, y: 0, width: w, height: h,
                        ref_width: w, ref_height: h,
                    });
                }
                return;
            }
            // rect が画像全体と等しい (= backend default) のに lockAspect が
            // ON なら、 ユーザー意図 (= lockAspectRatio に揃えたい) に合わせて補正。
            if (
                lockAspect
                && rect.x === 0 && rect.y === 0
                && rect.width === w && rect.height === h
            ) {
                const imgRatio = w / h;
                if (Math.abs(imgRatio - lockRatio) > 0.01) {
                    onChange({
                        ...fitRectToAspect(w, h, lockAspectRatio),
                        ref_width: w, ref_height: h,
                    });
                }
            }
        }
    };

    // 表示倍率 = 表示 width / natural width。
    const displayedRect = (): {
        scale: number;
        offsetX: number; offsetY: number;
    } | null => {
        if (!imgRef.current || !imgNatural) return null;
        const dispW = imgRef.current.clientWidth;
        const dispH = imgRef.current.clientHeight;
        if (dispW === 0 || dispH === 0) return null;
        return {
            scale: dispW / imgNatural.w,
            offsetX: imgRef.current.offsetLeft,
            offsetY: imgRef.current.offsetTop,
        };
    };

    const startDrag = (
        e: React.MouseEvent, mode: DragMode,
    ) => {
        if (!rect) return;
        e.preventDefault();
        e.stopPropagation();
        setDrag({
            mode,
            startClientX: e.clientX,
            startClientY: e.clientY,
            startRect: { ...rect },
        });
    };

    useEffect(() => {
        if (!drag || !rect || !imgNatural) return;
        let latestRect: TrimRect | null = null;
        const onMove = (e: MouseEvent) => {
            const dispInfo = displayedRect();
            if (!dispInfo) return;
            const dx = (e.clientX - drag.startClientX) / dispInfo.scale;
            const dy = (e.clientY - drag.startClientY) / dispInfo.scale;
            let { x, y, width, height } = drag.startRect;
            // lockAspect ON は「ラベルに表示している比率」 に固定する
            // (= startRect 比率の引き継ぎだと、 初期 rect がアス比違い
            // だった時にそのままズレ続ける)。
            const aspect = lockAspect
                ? lockRatio
                : drag.startRect.width / drag.startRect.height;
            const m = drag.mode;

            if (m === "move") {
                // move: rect 全体を平行移動、 画像端で押し戻り (= サイズ
                // 固定)、 アス比は変わらない。
                x = drag.startRect.x + dx;
                y = drag.startRect.y + dy;
                x = Math.max(
                    0, Math.min(x, imgNatural.w - drag.startRect.width),
                );
                y = Math.max(
                    0, Math.min(y, imgNatural.h - drag.startRect.height),
                );
                width = drag.startRect.width;
                height = drag.startRect.height;
            } else if (m !== null) {
                // resize: drag mode から「固定 corner / edge」 を決めて、
                // アス比固定 + 画像内収まる最大 size を計算する
                // (= まはー検証 2026-05-17、 旧 clamp は width/height を
                // 独立 clamp してアス比崩れる bug)。
                //
                // 1. mode から xDir / yDir (= 伸びる方向) と固定点を決定
                //    xDir = 1 → 右に伸びる (左固定)、 -1 → 左に伸びる (右固定)、
                //    0 → 上下のみ resize (中央固定)
                let xDir: -1 | 0 | 1 = 0;
                let yDir: -1 | 0 | 1 = 0;
                let fixedX = drag.startRect.x;
                let fixedY = drag.startRect.y;
                const startRight =
                    drag.startRect.x + drag.startRect.width;
                const startBottom =
                    drag.startRect.y + drag.startRect.height;
                switch (m) {
                    case "se":
                        xDir = 1; yDir = 1;
                        fixedX = drag.startRect.x;
                        fixedY = drag.startRect.y;
                        break;
                    case "ne":
                        xDir = 1; yDir = -1;
                        fixedX = drag.startRect.x;
                        fixedY = startBottom;
                        break;
                    case "sw":
                        xDir = -1; yDir = 1;
                        fixedX = startRight;
                        fixedY = drag.startRect.y;
                        break;
                    case "nw":
                        xDir = -1; yDir = -1;
                        fixedX = startRight;
                        fixedY = startBottom;
                        break;
                    case "e":
                        xDir = 1; yDir = 0;
                        fixedX = drag.startRect.x;
                        fixedY = drag.startRect.y
                            + drag.startRect.height / 2;
                        break;
                    case "w":
                        xDir = -1; yDir = 0;
                        fixedX = startRight;
                        fixedY = drag.startRect.y
                            + drag.startRect.height / 2;
                        break;
                    case "n":
                        xDir = 0; yDir = -1;
                        fixedX = drag.startRect.x
                            + drag.startRect.width / 2;
                        fixedY = startBottom;
                        break;
                    case "s":
                        xDir = 0; yDir = 1;
                        fixedX = drag.startRect.x
                            + drag.startRect.width / 2;
                        fixedY = drag.startRect.y;
                        break;
                }

                // 2. drag amount を適用した raw width/height。
                let newW = drag.startRect.width + xDir * dx;
                let newH = drag.startRect.height + yDir * dy;
                if (lockAspect) {
                    // 角 / 横方向 drag は width 主導、 縦のみ drag は height
                    // 主導 → 他方を aspect で算出。
                    if (xDir !== 0) {
                        newH = newW / aspect;
                    } else {
                        newW = newH * aspect;
                    }
                }

                // 3. 画像内に収まる最大 size をアス比制約付きで計算。
                //    各方向で「fixed 点から画像端までの距離」 = max 伸び量。
                let maxW: number;
                let maxH: number;
                if (xDir === 1) {
                    maxW = imgNatural.w - fixedX;
                } else if (xDir === -1) {
                    maxW = fixedX;
                } else {
                    // 中央固定: 左右両側に max(fixedX, imgNatural.w - fixedX)
                    maxW = 2 * Math.min(fixedX, imgNatural.w - fixedX);
                }
                if (yDir === 1) {
                    maxH = imgNatural.h - fixedY;
                } else if (yDir === -1) {
                    maxH = fixedY;
                } else {
                    maxH = 2 * Math.min(fixedY, imgNatural.h - fixedY);
                }
                // newW/newH は最大値を超えないように。
                newW = Math.max(10, Math.min(newW, maxW));
                newH = Math.max(10, Math.min(newH, maxH));
                // アス比固定なら、 縮んだ方に合わせて他方も縮める
                // (= 旧 bug の解消、 まはー検証 2026-05-17)。
                if (lockAspect) {
                    if (newW / aspect > newH) {
                        // height が制限要因 → width 再計算
                        newW = newH * aspect;
                    } else {
                        newH = newW / aspect;
                    }
                }

                // 4. 固定点 + xDir/yDir から rect 座標を算出。
                if (xDir === 1) {
                    x = fixedX; width = newW;
                } else if (xDir === -1) {
                    x = fixedX - newW; width = newW;
                } else {
                    x = fixedX - newW / 2; width = newW;
                }
                if (yDir === 1) {
                    y = fixedY; height = newH;
                } else if (yDir === -1) {
                    y = fixedY - newH; height = newH;
                } else {
                    y = fixedY - newH / 2; height = newH;
                }
            }
            // drag 中は内部 state にだけ反映 (= onChange = HTTP PATCH を
            // 60Hz で叩かないため)。
            const next = {
                x: Math.round(x), y: Math.round(y),
                width: Math.round(width), height: Math.round(height),
            };
            latestRect = next;
            setPendingRect(next);
        };
        const onUp = () => {
            // drag 終了で 1 回だけ commit (= 編集対象画像の natural size を
            // ref として添付、 backend の比例スケール適用用)。
            if (latestRect) {
                onChange({
                    ...latestRect,
                    ref_width: imgNatural.w,
                    ref_height: imgNatural.h,
                });
            }
            setPendingRect(null);
            setDrag(null);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
        return () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup", onUp);
        };
    }, [drag, rect, imgNatural, onChange, lockAspect, lockRatio]);

    // 表示用 rect (= 画像座標 → 表示 px)。 drag 中は pendingRect、
    // それ以外は parent から来る rect。
    const dispInfo = displayedRect();
    const overlayStyle: React.CSSProperties | null = (displayRect && dispInfo)
        ? {
            position: "absolute",
            left: dispInfo.offsetX + displayRect.x * dispInfo.scale,
            top: dispInfo.offsetY + displayRect.y * dispInfo.scale,
            width: displayRect.width * dispInfo.scale,
            height: displayRect.height * dispInfo.scale,
            border: "2px dashed var(--stackchan-info-border)",
            // 半透明オーバーレイ = 画像の上に「選択範囲」 を視覚化する目的で、
            // light / dark 両方で目立つ青系の固定値を使う。
            background: "rgba(80, 200, 255, 0.15)",
            cursor: drag?.mode === "move" ? "grabbing" : "grab",
            boxSizing: "border-box",
        }
        : null;

    if (!imageSrc) {
        return (
            <div style={styles.trimVisualEmpty}>
                参照画像なし (= ② を実行してから ④ で trim 範囲を決める)
            </div>
        );
    }

    return (
        <div style={styles.trimVisualWrap}>
            <div style={styles.row}>
                <label style={styles.checkboxLabel}>
                    <input
                        type="checkbox"
                        checked={lockAspect}
                        onChange={(e) => onLockAspectChange(e.target.checked)}
                    />
                    {lockAspectRatio} アス比固定{
                        lockAspectRatio === "4:3"
                            ? " (= ⑤ 160×120 出力で歪まない)"
                            : ""
                    }
                </label>
                {imgNatural && displayRect && (
                    <span style={styles.subtle}>
                        画像 {imgNatural.w}×{imgNatural.h} /
                        rect ({displayRect.x},{displayRect.y})
                        {displayRect.width}×{displayRect.height}
                    </span>
                )}
            </div>
            <div ref={containerRef} style={styles.trimVisualStage}>
                <img
                    ref={imgRef}
                    src={imageSrc}
                    alt="trim source"
                    onLoad={handleImgLoad}
                    style={styles.trimVisualImage}
                />
                {overlayStyle && (
                    <>
                        <div
                            style={overlayStyle}
                            onMouseDown={(e) => startDrag(e, "move")}
                        >
                            {/* 8 handle */}
                            {(["nw", "n", "ne", "e", "se", "s", "sw", "w"] as DragMode[])
                                .map((h) => (
                                    <div
                                        key={h ?? "x"}
                                        style={handleStyle(h)}
                                        onMouseDown={(e) => startDrag(e, h)}
                                    />
                                ))}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

function handleStyle(mode: DragMode): React.CSSProperties {
    const base: React.CSSProperties = {
        position: "absolute",
        width: "10px",
        height: "10px",
        // ハンドル本体 = 青背景 + 白フチ。 白フチは「青背景に対する固定の
        // 縁取り」 として light / dark 共通で機能するためハードコード維持。
        background: "var(--stackchan-info-border)",
        border: "1px solid #fff",
        boxSizing: "border-box",
    };
    // 位置 + cursor。
    switch (mode) {
        case "nw":
            return { ...base, left: -5, top: -5, cursor: "nwse-resize" };
        case "n":
            return {
                ...base, left: "calc(50% - 5px)", top: -5,
                cursor: "ns-resize",
            };
        case "ne":
            return { ...base, right: -5, top: -5, cursor: "nesw-resize" };
        case "e":
            return {
                ...base, right: -5, top: "calc(50% - 5px)",
                cursor: "ew-resize",
            };
        case "se":
            return { ...base, right: -5, bottom: -5, cursor: "nwse-resize" };
        case "s":
            return {
                ...base, left: "calc(50% - 5px)", bottom: -5,
                cursor: "ns-resize",
            };
        case "sw":
            return { ...base, left: -5, bottom: -5, cursor: "nesw-resize" };
        case "w":
            return {
                ...base, left: -5, top: "calc(50% - 5px)",
                cursor: "ew-resize",
            };
        default:
            return base;
    }
}

// ----- ④ Trim rect editor -----

function TrimRectEditor({
    rect, onChange, sampleImageUrl,
}: {
    rect: TrimRect | null;
    onChange: (rect: TrimRect) => void;
    sampleImageUrl: string | null;
}) {
    // 自動保存: onBlur で 4 値まとめて save (= 矩形保存ボタン廃止)。
    const [x, setX] = useState(rect?.x ?? 0);
    const [y, setY] = useState(rect?.y ?? 0);
    const [w, setW] = useState(rect?.width ?? 1024);
    const [h, setH] = useState(rect?.height ?? 768);
    const [lockAspect, setLockAspect] = useState<boolean>(true);
    // 編集対象画像の natural size (= 数値 input 経由の保存にも ref を付与)。
    const [refDims, setRefDims] = useState<{ w: number; h: number } | null>(
        null,
    );

    useEffect(() => {
        if (rect) {
            setX(rect.x); setY(rect.y);
            setW(rect.width); setH(rect.height);
        }
    }, [rect?.x, rect?.y, rect?.width, rect?.height]);

    const saveIfDirty = () => {
        const dirty = !rect || rect.x !== x || rect.y !== y
            || rect.width !== w || rect.height !== h;
        if (dirty) {
            const next: TrimRect = { x, y, width: w, height: h };
            if (refDims) {
                next.ref_width = refDims.w;
                next.ref_height = refDims.h;
            }
            onChange(next);
        }
    };

    return (
        <div style={styles.trimSection}>
            <div style={styles.label}>
                トリミング矩形 (= 編集中プレビュー画像での座標、 他の画像
                にはサイズ比でスケールして適用、 自動保存):
            </div>
            <TrimRectVisualEditor
                imageSrc={sampleImageUrl}
                rect={rect}
                onChange={onChange}
                lockAspect={lockAspect}
                onLockAspectChange={setLockAspect}
                onImageSize={(iw, ih) => setRefDims({ w: iw, h: ih })}
            />
            <div style={styles.row}>
                <label>x:</label>
                <input
                    type="number" value={x} style={styles.numberInput}
                    onChange={(e) => setX(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label>y:</label>
                <input
                    type="number" value={y} style={styles.numberInput}
                    onChange={(e) => setY(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label>width:</label>
                <input
                    type="number" value={w} style={styles.numberInput}
                    onChange={(e) => setW(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
                <label>height:</label>
                <input
                    type="number" value={h} style={styles.numberInput}
                    onChange={(e) => setH(parseInt(e.target.value) || 0)}
                    onBlur={saveIfDirty}
                />
            </div>
        </div>
    );
}

// ----- zip 投入セクション (= ④から開始経路) -----

function ZipImportSection({
    mode, onImport,
}: {
    mode: "matrix" | "layered";
    onImport: (file: File, requireComplete: boolean) => Promise<unknown>;
}) {
    const [file, setFile] = useState<File | null>(null);
    const [requireComplete, setRequireComplete] = useState(true);
    const expectedCount = mode === "matrix" ? 90 : 14;

    return (
        <div style={styles.trimSection}>
            <div style={styles.label}>
                zip / フォルダ直接投入 (= ①②③ をスキップして手持ち画像で
                ④ から開始):
            </div>
            <div style={styles.subtle}>
                zip 内に {expectedCount} 個の PNG (= mode={mode}) が必要。
                ファイル名規約: {
                    mode === "matrix"
                        ? "{face}_{eyes}_{mouth}.png"
                        : "face_<name>.png / eyes_<state>.png / mouth_<shape>.png"
                }
            </div>
            <div style={styles.row}>
                <input
                    type="file"
                    accept=".zip,application/zip"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                    style={styles.fileInput}
                />
                <label style={styles.checkboxLabel}>
                    <input
                        type="checkbox"
                        checked={requireComplete}
                        onChange={(e) => setRequireComplete(e.target.checked)}
                    />
                    完全性チェック (= 不足あればエラー)
                </label>
                <button
                    onClick={() => file && onImport(file, requireComplete)}
                    disabled={!file}
                    style={
                        file ? styles.btnPrimary : styles.btnDisabled
                    }
                >
                    投入
                </button>
            </div>
        </div>
    );
}

// ----- Inline styles -----

// 色は本体 globals.css の --bg-X / --text-X / --border-color と、
// アドオン固有の --stackchan-X (theme.module.css 参照) を使い、
// light / dark テーマ切替に追従する。
const styles: Record<string, React.CSSProperties> = {
    overlay: {
        position: "fixed",
        inset: 0,
        // モーダルオーバーレイは light/dark 共に半透明黒で
        // 背景を抑える効果は同じなのでハードコード維持。
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
    },
    modal: {
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        borderRadius: "6px",
        padding: "16px",
        width: "90vw",
        maxWidth: "1200px",
        maxHeight: "90vh",
        overflowY: "auto",
        border: "1px solid var(--border-color)",
        fontSize: "12px",
    },
    header: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
        marginBottom: "12px",
        paddingBottom: "8px",
        borderBottom: "1px solid var(--border-color)",
    },
    title: {
        fontSize: "16px",
        fontWeight: 600,
    },
    subtitle: {
        fontSize: "11px",
        color: "var(--text-secondary)",
        marginTop: "2px",
    },
    activeBadge: {
        marginLeft: "8px",
        padding: "1px 6px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    closeBtn: {
        background: "transparent",
        color: "var(--text-secondary)",
        border: "none",
        fontSize: "20px",
        cursor: "pointer",
        padding: "0 8px",
    },
    busyBox: {
        marginBottom: "8px",
        padding: "6px 10px",
        background: "var(--stackchan-info-soft-bg)",
        color: "var(--stackchan-info-soft-fg)",
        borderRadius: "4px",
    },
    chainBox: {
        marginBottom: "8px",
        padding: "6px 10px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderRadius: "4px",
        fontSize: "11px",
    },
    chainFailed: {
        color: "var(--stackchan-warning-fg)",
    },
    errorBox: {
        marginBottom: "8px",
        padding: "6px 10px",
        background: "var(--stackchan-danger-soft-bg)",
        color: "var(--stackchan-danger-soft-fg)",
        borderRadius: "4px",
    },
    loading: { padding: "20px", textAlign: "center", color: "var(--text-secondary)" },
    section: {
        marginBottom: "12px",
        padding: "8px 12px",
        background: "var(--bg-tertiary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    sectionTitle: {
        fontSize: "12px",
        fontWeight: 600,
        color: "var(--text-primary)",
        cursor: "pointer",
        marginBottom: "8px",
    },
    textarea: {
        width: "100%",
        padding: "6px",
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontFamily: "inherit",
        fontSize: "11px",
        boxSizing: "border-box",
    },
    row: {
        display: "flex",
        alignItems: "center",
        gap: "8px",
        marginTop: "8px",
        flexWrap: "wrap",
    },
    label: { color: "var(--text-secondary)", fontSize: "11px" },
    select: {
        padding: "3px 6px",
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontSize: "11px",
    },
    numberInput: {
        width: "70px",
        padding: "3px 6px",
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontSize: "11px",
    },
    numberInputSmall: {
        width: "55px",
        padding: "2px 4px",
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    variantList: {
        display: "flex",
        flexDirection: "column" as const,
        gap: "6px",
        marginTop: "6px",
    },
    variantRowCollapsed: {
        display: "flex",
        alignItems: "center",
        gap: "8px",
        padding: "6px 8px",
        background: "var(--bg-secondary)",
        borderRadius: "3px",
        border: "1px solid var(--border-color)",
        fontSize: "11px",
    },
    variantRowExpanded: {
        display: "flex",
        flexDirection: "column" as const,
        gap: "6px",
        padding: "8px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--stackchan-info-border)",
    },
    variantLabel: {
        fontFamily: "monospace",
        fontWeight: 600,
        color: "var(--stackchan-info-soft-fg)",
        minWidth: "100px",
    },
    stageBar: {
        display: "flex",
        gap: "4px",
        margin: "12px 0",
        flexWrap: "wrap",
    },
    stageBtn: {
        padding: "6px 12px",
        background: "var(--bg-hover)",
        color: "var(--text-secondary)",
        border: "1px solid var(--border-color)",
        borderRadius: "4px",
        cursor: "pointer",
        fontSize: "11px",
    },
    stageBtnActive: {
        background: "var(--stackchan-info-soft-bg)",
        color: "var(--stackchan-info-soft-fg)",
        borderColor: "var(--stackchan-info-border)",
    },
    stageBtnComplete: {
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderColor: "var(--stackchan-success-border)",
    },
    stageBtnCount: {
        opacity: 0.7,
        fontSize: "10px",
    },
    stageBody: {
        marginTop: "8px",
        padding: "8px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    stageHeader: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        marginBottom: "8px",
    },
    stageHeaderTitle: {
        fontSize: "13px",
        fontWeight: 600,
    },
    stageDescription: {
        fontSize: "11px",
        color: "var(--text-secondary)",
        marginTop: "2px",
        lineHeight: 1.4,
    },
    stageHeaderActions: { display: "flex", gap: "6px" },
    completedBadge: {
        marginLeft: "8px",
        padding: "1px 6px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    targetGrid: {
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
        gap: "8px",
        marginTop: "8px",
    },
    matrixGroupContainer: {
        display: "flex",
        flexDirection: "column",
        gap: "12px",
        marginTop: "8px",
    },
    matrixFaceGroup: {
        padding: "8px",
        background: "var(--bg-tertiary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    matrixFaceHeader: {
        fontSize: "12px",
        fontWeight: 600,
        color: "var(--text-primary)",
        marginBottom: "4px",
    },
    targetCell: {
        padding: "6px",
        background: "var(--bg-tertiary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
        display: "flex",
        flexDirection: "column",
        gap: "4px",
    },
    preview: {
        // 画像プレビューは黒背景固定 (= alpha 抜き透明画像の判別のため、
        // light でも黒のほうが視認しやすい)。 出力画像のアスペクト比 (=
        // metadata.aspect_ratio の default "4:3") に合わせて枠を作り、
        // contain で黒帯が出ないようにする。
        width: "100%",
        aspectRatio: "4 / 3",
        objectFit: "contain",
        background: "var(--stackchan-code-bg)",
        borderRadius: "3px",
        cursor: "zoom-in",
    },
    lightboxOverlay: {
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.92)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 10001,
        cursor: "zoom-out",
    },
    lightboxInner: {
        maxWidth: "95vw",
        maxHeight: "95vh",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
        cursor: "default",
    },
    lightboxImage: {
        maxWidth: "95vw",
        maxHeight: "85vh",
        objectFit: "contain",
        background: "var(--stackchan-code-bg)",
        borderRadius: "4px",
    },
    lightboxFooter: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: "12px",
        color: "var(--text-primary)",
        fontSize: "12px",
    },
    lightboxAlt: {
        fontFamily: "monospace",
    },
    previewEmpty: {
        // 「未生成」 セルも 4:3 にして、 画像有セルと高さを揃える。
        width: "100%",
        aspectRatio: "4 / 3",
        background: "var(--bg-tertiary)",
        borderRadius: "3px",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--text-secondary)",
        fontSize: "10px",
    },
    targetLabel: {
        fontSize: "10px",
        color: "var(--text-secondary)",
        textAlign: "center",
        fontFamily: "monospace",
    },
    baseReuseBadge: {
        fontSize: "9px",
        color: "var(--stackchan-info-soft-fg)",
        textAlign: "center",
        padding: "2px 0",
    },
    targetExtraInput: {
        width: "100%",
        padding: "4px",
        background: "var(--bg-secondary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontFamily: "inherit",
        fontSize: "10px",
        boxSizing: "border-box",
    },
    regenBtn: {
        padding: "3px 6px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "10px",
    },
    regenBtnBusy: {
        padding: "3px 6px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        border: "1px solid var(--stackchan-success-border)",
        borderRadius: "3px",
        cursor: "wait",
        fontSize: "10px",
    },
    trimSection: {
        margin: "8px 0",
        padding: "8px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
    },
    trimVisualWrap: {
        display: "flex",
        flexDirection: "column" as const,
        gap: "6px",
        margin: "6px 0",
    },
    trimVisualStage: {
        position: "relative" as const,
        display: "inline-block",
        background: "var(--stackchan-code-bg)",
        borderRadius: "3px",
        maxWidth: "100%",
        userSelect: "none" as const,
    },
    trimVisualImage: {
        display: "block",
        maxWidth: "100%",
        maxHeight: "320px",
    },
    trimVisualEmpty: {
        padding: "12px",
        color: "var(--stackchan-danger-soft-fg)",
        fontSize: "11px",
        textAlign: "center" as const,
        border: "1px dashed var(--stackchan-danger-border)",
        borderRadius: "3px",
    },
    finalSection: {
        margin: "12px 0",
        padding: "10px",
        background: "var(--stackchan-success-soft-bg)",
        borderRadius: "4px",
        border: "1px solid var(--stackchan-success-border)",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
    },
    finalTitle: {
        fontSize: "12px",
        fontWeight: 600,
        color: "var(--stackchan-success-soft-fg)",
    },
    navBar: {
        display: "flex",
        justifyContent: "space-between",
        marginTop: "12px",
        paddingTop: "8px",
        borderTop: "1px solid var(--border-color)",
    },
    btn: {
        padding: "4px 12px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    btnPrimary: {
        padding: "4px 12px",
        background: "var(--stackchan-success-strong-bg)",
        color: "var(--stackchan-success-strong-fg)",
        border: "1px solid var(--stackchan-success-border)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    btnAccent: {
        padding: "4px 12px",
        background: "var(--stackchan-warning-soft-bg)",
        color: "var(--stackchan-warning-soft-fg)",
        border: "1px solid var(--stackchan-warning-border)",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "11px",
    },
    btnDisabled: {
        padding: "4px 12px",
        background: "var(--bg-tertiary)",
        color: "var(--text-secondary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        cursor: "not-allowed",
        fontSize: "11px",
        opacity: 0.6,
    },
    subtle: {
        color: "var(--text-secondary)",
        fontSize: "10px",
    },
    fileInput: {
        flex: 1,
        padding: "3px",
        background: "var(--bg-tertiary)",
        color: "var(--text-primary)",
        border: "1px solid var(--border-color)",
        borderRadius: "3px",
        fontSize: "11px",
    },
    checkboxLabel: {
        display: "flex",
        alignItems: "center",
        gap: "4px",
        color: "var(--text-secondary)",
        fontSize: "11px",
    },
    debugTag: {
        marginLeft: "8px",
        padding: "1px 6px",
        background: "var(--stackchan-warning-soft-bg)",
        color: "var(--stackchan-warning-soft-fg)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    autosaveTag: {
        marginLeft: "8px",
        padding: "1px 6px",
        background: "var(--stackchan-success-soft-bg)",
        color: "var(--stackchan-success-soft-fg)",
        borderRadius: "3px",
        fontSize: "10px",
    },
    debugBox: {
        margin: "8px 0",
        padding: "6px 8px",
        background: "var(--stackchan-danger-soft-bg)",
        borderRadius: "4px",
        border: "1px dashed var(--stackchan-danger-border)",
    },
    subpanel: {
        margin: "8px 0",
        padding: "8px",
        background: "var(--bg-secondary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
    },
    refGrid: {
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
        gap: "6px",
    },
    refCell: {
        padding: "6px",
        background: "var(--bg-tertiary)",
        borderRadius: "4px",
        border: "1px solid var(--border-color)",
        fontSize: "10px",
    },
    refCellSelected: {
        background: "var(--stackchan-info-soft-bg)",
        border: "1px solid var(--stackchan-info-border)",
    },
    refName: {
        color: "var(--text-primary)",
        fontFamily: "monospace",
        wordBreak: "break-all" as const,
    },
    animPreviewLayout: {
        display: "flex",
        gap: "12px",
        alignItems: "flex-start",
    },
    animStage: {
        width: "320px",
        height: "240px",
        background: "var(--stackchan-code-bg)",
        borderRadius: "4px",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        position: "relative" as const,
        overflow: "hidden" as const,
        flexShrink: 0,
    },
    animImage: {
        maxWidth: "100%",
        maxHeight: "100%",
        objectFit: "contain" as const,
    },
    animLayerStack: {
        position: "relative" as const,
        width: "100%",
        height: "100%",
    },
    animLayerImg: {
        position: "absolute" as const,
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        objectFit: "contain" as const,
    },
    animMissing: {
        color: "var(--stackchan-danger-soft-fg)",
        fontSize: "11px",
        padding: "12px",
        textAlign: "center" as const,
    },
    animControls: {
        flex: 1,
        display: "flex",
        flexDirection: "column" as const,
        gap: "6px",
    },
};
