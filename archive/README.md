# archive/ — v0.4 までの自前ファーム + 自前 gateway 資産

このディレクトリには、SAIVerse-stackchan-addon が v0.5 で stackchan-mcp
(kisaragi-mochi/stackchan-mcp, xiaozhi-esp32 ベース) のエコシステムに乗り換え
る前の、自前ファーム + 自前 WebSocket gateway 経路の実装一式を保存する。

active な開発はしない。将来「stackchan-mcp が満たさない vessel 要件」が出て
きた時の出発点になる (= 別 board での独自 audio パス、PCM 直送が必要な低レイ
テンシ用途、xiaozhi-esp32 が対応しない特殊ハードウェア等)。

## 経緯

- v0.1 〜 v0.4: 自前ファーム + 自前 gateway で進めていた。Phase 1 (ペアリング
  + WS) と Phase 2-D (voice-tts → PCM 直送 → device の playRaw) まで実機で
  動作確認済み (voice-tts → 物理スピーカーの音声経路が成立、connection 切れ
  なし、frame drop なし)
- v0.5 (2026-05-13): stackchan-mcp 採用への路線変更。判断軸は「stackchan-mcp
  が既に実装している機能を自前で再実装する必要がない」「唯一の欠け (TTS) は
  Phase 2 で完成した経路の宛先切り替えで埋まる」「将来にわたって作業量が最短」。
  詳細は SAIVerse 本体側 `docs/intent/stackchan_vessel.md` v0.5 (commit
  `111c920`) §「なぜ stackchan-mcp に乗り換えるか」参照

## ここに残っているもの

| パス | 概要 |
|---|---|
| `api_routes.py` | 自前 FastAPI router (HTTP API ペアリング `/pair`, vessel 一覧 `/vessels`, WebSocket gateway `/vessel`)。v0.5 では gateway 側 WS server を stackchan-mcp gateway に移譲、HTTP API ペアリング部分は Phase 2' で再構築予定 |
| `audio_stream_bridge.py` | voice-tts の subscribe_pcm → 自前ファームへの WS binary frame 直送ブリッジ。 sub chunk 8 KB 分割 + sample_rate × 2 bytes/sec の pacing (lead_time 200ms) + identity-aware abort 制御の知見を含む |
| `firmware/` | M5Stack CoreS3 用の自前ファーム (Arduino + M5Unified + WebSocketsClient)。PCM 直送 + ring buffer 32 KB + rotation buffer 4×32KB + identity-aware unregister 等の Phase 2-D で確立した実装。`dist/` 配下にビルド済み .bin (bootloader / partitions / firmware) あり |
| `setup_ui/` | Web Serial フラッシュ用の静的 HTML (esptool-js ベース、ブラウザから .bin を書き込み)。v0.5 では SAIVerse CLI が esptool を直接呼ぶ方式に置き換え |

`firmware/` は本ディレクトリには移動できなかった (PlatformIO Home の VS Code
session が掴んでいる Windows のファイルロック問題)。元の場所 `../firmware/`
に残っている。後で PlatformIO Home を一旦停止してから archive/ へ移動する
予定 (移動が間に合わなくても addon の動作には影響しない)。

## v0.5 の正規パス

archive じゃない、現役の実装は addon ルート (`../`) の以下:

- `mcp_servers.json`: stackchan-mcp gateway を本体 MCP client が subprocess
  起動する設定 (Elyth と同じ枠組み)
- `addon.json`: AddonConfig の `params_schema` (master_token, pcm_token,
  gateway_host/ws_port/capture_port, vision_host)
- `speak_hook.py`: persona_speak → voice-tts.subscribe_pcm → HTTP POST
  (chunked transfer) で gateway の `/pcm` endpoint に送信
- `vessel_manager.py`: vessels.db で token_hash → vessel_id → bound_building_id
  の対応テーブル管理

詳細設計: SAIVerse 本体側 `docs/intent/stackchan_vessel.md` v0.5。

## 知見の継承先

Phase 2-D で得た以下の知見は、現役 v0.5 経路ではほぼ不要になったが、将来別の
vessel addon が出てきた時の参照として残す:

- **PCM 直送 + Opus decode 廃止**: libhelix の frame sync 失敗 + playRaw の
  data 寿命管理の罠で発話が壊れる問題、PCM 直送に切り替えて解決
- **8 KB WS frame 分割**: ESP32 WebSocketsClient のデフォルト 15 KB silent
  disconnect を回避
- **PCM rotation buffer (4 個 × 32 KB)**: M5.Speaker.playRaw が data を内部
  コピーしない + xRingbufferReceive が連続データをまとめて返す挙動への対応
- **sub chunk 単位 pacing**: TTS engine の generator chunk (= 数秒分の PCM) を
  そのまま burst 送信すると device 側 ring buffer が overflow する問題への
  対応
- **identity-aware unregister**: TCP half-open で古い task が新 session を誤
  削除するゾンビ状態の防止

これらは `docs/issues/websocket_session_registry.md` (= 物理 Vessel SDK 共通
基盤化案件、SAIVerse 本体側) にも反映されている。
