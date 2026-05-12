# saiverse-stackchan-addon

SAIVerse のペルソナを [Stack-chan](https://github.com/stack-chan/stack-chan) (M5Stack 製 AI Desktop Robot) の物理身体に降ろす **Vessel 統合アドオン**。

Vessel Building にペルソナが居る間、物理マイク・スピーカー・サーボ・カメラ・タッチパネルがそのペルソナの身体感覚として動作する。

## 状態

**開発中 (Phase 1)** — WebSocket Gateway とペアリング HTTP API のサーバ側実装まで完了。ユーザー導入経路 (AddonManager UI ペアリングパネル / Web Serial ベースのファーム書き込み) とファームウェア (M5Unified + WebSocket クライアント) は順次追加予定。

## 詳細設計

設計思想・認知モデル・不変条件・ロードマップは SAIVerse 本体側の Intent Document を参照:

[`docs/intent/stackchan_vessel.md`](https://github.com/maha0525/SAIVerse/blob/feature/memory-notes-and-organize/docs/intent/stackchan_vessel.md)

中核となる認知モデル: **Vessel Building 全体 = 身体、ペルソナ = 脳/魂**。マイクは耳、STT は聴覚野、スピーカーは口、TTS は発声、カメラは目、サーボは姿勢、タッチは触覚にマッピングされる。

## 対応ハードウェア

- **M5Stack 製 StackChan AI Desktop Robot** (M5Core2/CoreS3 ベース、SKU 11129)
- https://www.switch-science.com/products/11129

将来的に別機種 (眼鏡型ウェアラブル、別ロボット等) への対応も Vessel 共通仕様として検討する。

## SAIVerse 本体側の要件

- `Building` テーブルに `PHYSICAL_VESSEL_ID` カラム (本アドオンを使う SAIVerse バージョンで自動マイグレーション)
- 既存のアドオン拡張点 (`server_hooks`, `api_routes.py` 自動マウント, `addon_paths.get_addon_storage_path`, `addon_deps.get_manager`)
- 既存の voice-tts アドオンの `audio_stream` pub/sub (Phase 2 で TTS ストリーミングに相乗り)

## ライセンス

Apache License 2.0
