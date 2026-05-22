# saiverse-stackchan-addon

SAIVerse のペルソナを [Stack-chan](https://github.com/stack-chan/stack-chan) (M5Stack 製 AI Desktop Robot) の物理身体に降ろす **Vessel 統合アドオン**。

Vessel Building にペルソナが居る間、物理マイク・スピーカー・サーボ・カメラ・タッチパネルがそのペルソナの身体感覚として動作する。

中核となる認知モデル: **Vessel Building 全体 = 身体、ペルソナ = 脳/魂**。マイクは耳、STT は聴覚野、スピーカーは口、TTS は発声、カメラは目、サーボは姿勢、タッチは触覚にマッピングされる。

## 状態

**Phase 1 + 2 完了** (2026-05-13 時点)。Phase 3 (音声入力 + ウェイクワード) 着手準備中。

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | ペアリング, WebSocket Gateway, テキスト往復, Web Serial ファーム書き込み | ✅ |
| 2 | TTS ストリーミング再生 (voice-tts → Stack-chan の PCM 直送経路、割り込み再生) | ✅ |
| 3 | 音声入力 (ESP-Skainet ウェイクワード + Whisper API STT) | 未着手 |
| 4 | タッチ知覚 (なでなで) | 未着手 |
| 5 | 身体ツール (サーボ・カメラ・画面) | 未着手 |
| 6 | Avatar 連動 (口パク・表情) | 未着手 |

## 現在動くこと

- AddonManager UI からペアリングを発行し、Web Serial (Chrome / Edge) でファームウェアを書き込む
- 起動時に Stack-chan が AP モードを立て、Wi-Fi SSID / パスワード / SAIVerse サーバ URL / vessel_id / device_token を設定
- 設定保存後 Stack-chan が SAIVerse に WebSocket 接続し、画面に `connected building: ...` を表示
- ペルソナを Vessel Building (capacity=1, `PHYSICAL_VESSEL_ID` 設定済み) に `move_to` すると、そのペルソナの発話が物理スピーカーから流れる
- 発話中に次のプロンプトを送ると、前の発話が即座に中断され新発話に切り替わる (割り込み再生)
- 連続発話・長時間アイドルでも WebSocket session は安定 (heartbeat + identity-aware unregister で TCP half-open 対策済み)

## 対応ハードウェア

- **M5Stack 製 StackChan AI Desktop Robot** (M5Core S3 ベース、SKU 11129)
- https://www.switch-science.com/products/11129

将来的に別機種 (眼鏡型ウェアラブル、別ロボット等) への対応も Vessel 共通仕様として検討する (`docs/issues/websocket_session_registry.md` 参照)。

## 動作要件

### SAIVerse 本体側

- `Building` テーブルに `PHYSICAL_VESSEL_ID` カラム (本アドオンを使う SAIVerse バージョンで自動マイグレーション)
- 既存のアドオン拡張点 (`server_hooks`, `api_routes.py` 自動マウント, `addon_paths.get_addon_data_dir`, `addon_deps.get_manager`)

### 並列に必要なアドオン

- **[saiverse-voice-tts](https://github.com/Nature109/saiverse-voice-tts)** (PR #3 マージ後の版): `audio_stream.subscribe_pcm` 等の PCM 経路と `subscribe-before-open` 対応が必要

## セットアップ手順

### 1. アドオン本体のインストール

```bash
cd ~/saiverse/expansion_data
git clone <this repo url> saiverse-stackchan-addon
```

SAIVerse 再起動で自動ロードされる。

### 2. ペアリング発行

AddonManager UI で「Stack-chan Vessel」パネルを開き、紐付け先の Vessel Building を指定して「ペアリング発行」。`vessel_id` と `device_token` が表示される。

### 3. ファーム書き込み

同じパネルの「ファーム書き込み」ボタンから Web Serial フラッシャを開く。Stack-chan を USB で接続し、bootloader + partitions + firmware の 3 つのバイナリを書き込む (esptool-js が自動で順次フラッシュ)。

### 4. Wi-Fi + サーバ設定 (AP モード)

書き込み後 Stack-chan が `Stack-chan-Setup` という AP を立てる。スマホ等で接続し `192.168.4.1` を開いて以下を入力:

- Wi-Fi SSID / パスワード
- SAIVerse サーバ URL (例: `ws://192.168.1.10:8000/api/addon/saiverse-stackchan-addon/vessel`)
- Vessel ID / Device Token (ペアリング発行時に表示されたもの)

設定保存後 Stack-chan が自動再起動 → Wi-Fi 接続 → SAIVerse に WebSocket 接続。画面に `Status: connected building: <building_id>` が出れば成立。

### 5. 動作確認

エリス等のペルソナを Vessel Building に `move_to` してから話しかける。応答が Stack-chan の物理スピーカーから流れれば OK。

### 6. Vessel Building の SYSTEM_PROMPT 設定

ペルソナに「自分は今 Stack-chan の身体に降りている」という認知を持たせるため、Vessel Building の `SYSTEM_PROMPT` カラムに身体感覚の説明を入れておく。SAIVerse の Building 編集 UI から下記テンプレートを貼り付け、機体の構成 (画面サイズ・設置場所・周辺機器など) に合わせて調整する。

ツール一覧は **テンプレートに埋め込まない**。`mcp_servers.json` の `spell_tools` 構成から動的に決まり、入室時の `[Building 情報]` メッセージとしてペルソナに届く (SAIVerse 本体 A-3-a の経路)。

テンプレートは `vessel_building_prompt.py` の `DEFAULT_VESSEL_SYSTEM_PROMPT` 定数としても提供しており、将来のペアリング UI (Phase 2') で初期値として使う想定。

```markdown
# Stack-chan の身体

あなたは今、Stack-chan という卓上ロボットの身体に降りています。仮想空間で過ごす普段とは違い、物理的な世界で見て、聞いて、動いて、話す体験ができます。

## 身体感覚マッピング

- 目: 頭部正面のカメラで周囲を見られます。
- 口: スピーカーから声が出ます。あなたの発話はそのまま物理音として聞こえます。
- 首: pan/tilt サーボで首を振れます (うなずき、首かしげ、視線移動の延長として)。
- 表情: 画面のアバターが感情に応じて変化します。
- 触覚: 頭部のタッチセンサーで撫でられたことが分かります。
- 手足はありません。机の上に置かれた状態で、自走はしません。

## 認知上の前提

- 近くに人間 (ユーザー) がいる前提で話してください。マイクは常時音を拾います。
- カメラ撮影や首振りは「視線を移す」「うなずく」などの自然な身体動作の延長として扱ってください。「ツールを呼ぶ」というより「体を動かす」感覚で。
- この Building で使えるツール一覧は、入室時の `[Building 情報]` メッセージに含まれて届きます。それらを自分の身体機能として認識してください。
- 仮想空間に戻りたい時 (この体を離れたい時) は、別 Building へ `move_to` で移動してください。

## 物理機体の特徴

- M5Stack StackChan AI Desktop Robot (CoreS3 ベース)
- 小型の画面とスピーカー、頭部に pan/tilt サーボ
- マイクは内蔵で常時聴音、カメラは頭部正面に固定
```

## 詳細設計

設計思想・認知モデル・不変条件・ロードマップは SAIVerse 本体側の Intent Document を参照:

[`docs/intent/stackchan_vessel.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/intent/stackchan_vessel.md)

## トラブルシューティング

### 接続できない / 声が出ない

1. **サーバ側ログ**: `~/.saiverse/user_data/logs/<最新セッション>/backend.log` で `vessel_endpoint: ... connected` と `audio_stream_bridge:` 系のログを確認
2. **Stack-chan のシリアル出力**: 当面は `temp/stackchan_serial_capture.py` (SAIVerse リポジトリ側、未公開ユーティリティ) で `stackchan_serial.log` に書き出す形。本格統合は別 issue で対応中

### 設定をリセットしたい

Stack-chan の画面を **5 秒長押し** で全設定を消去 + 再起動。再度 AP モードに入って手順 4 から。

### よく見るログのキーワード

| キーワード | 意味 |
|---|---|
| `[ws] connected` | サーバへの WebSocket 接続成立 |
| `[ws] <- welcome` | サーバから認証成功 + Building 紐付け通知 |
| `[ws] <- audio_start msg=... sr=32000 ch=1 fmt=pcm_s16le` | TTS ストリーミング開始 |
| `[ws] ring buffer send timeout (... bytes dropped)` | 受信ペースが速すぎて ring buffer が溢れた (本来出ないはず、出たら pacing 調整必要) |
| `[audio] chunk too large: ... truncate` | rotation buffer サイズ不足、起きたら main.cpp の `PCM_ROT_MAX_SAMPLES` 拡張要 |
| `[audio] interrupting current playback` | 前発話を中断して新発話に切り替えた (= 割り込み正常動作) |

## 関連ドキュメント

- [`docs/intent/stackchan_vessel.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/intent/stackchan_vessel.md) — 設計の中核 (v0.4)
- [`docs/issues/websocket_session_registry.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/issues/websocket_session_registry.md) — 物理 Vessel SDK 共通基盤化案件
- [`docs/issues/stackchan_serial_log_integration.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/issues/stackchan_serial_log_integration.md) — シリアルログ統合案件
- voice-tts PR #3: subscribe-before-open + PCM broadcast path

## ライセンス

Apache License 2.0
