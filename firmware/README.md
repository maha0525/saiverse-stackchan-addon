# Stack-chan Vessel Firmware

SAIVerse Stack-chan Vessel アドオン用 ESP32-S3 ファームウェア。

**Target:** M5Stack CoreS3 (StackChan AI Desktop Robot 同梱基板)

## ビルド (PlatformIO)

PlatformIO Core (CLI) または VSCode 拡張で開く。

```bash
cd firmware/
pio run                  # ビルドのみ → .pio/build/m5stack-cores3/firmware.bin
pio run -t upload        # USB 経由で書き込み
pio device monitor       # シリアルモニタ (115200)
```

PlatformIO 未導入の場合:

```bash
pip install platformio
```

## Web Serial 配布

`pio run` で生成された `.pio/build/m5stack-cores3/firmware.bin` を `../dist/` にコピーし、リポジトリのリリースタグに同梱する。ユーザーはブラウザ (Chrome/Edge) の Web Serial ベースのフラッシュページから書き込める。

## 動作フロー (Phase 1)

1. 起動 → M5Unified 初期化
2. Preferences から保存設定読込 (`server_url` / `vessel_id` / `device_token`)
3. 設定がなければ WiFiManager AP モード起動 (`Stack-chan-Setup`)
   - PC/スマホで AP に接続し、`192.168.4.1` で開く設定 UI から:
     - Wi-Fi SSID / Password
     - SAIVerse Server URL (例: `ws://192.168.1.10:3000/api/addon/saiverse-stackchan-addon/vessel`)
     - Vessel ID (SAIVerse 側 AddonManager の「ペアリング発行」で取得)
     - Device Token (同上)
4. Wi-Fi 接続後 WebSocket 接続 → `hello` 送信
5. SAIVerse から `welcome` 受信で認証完了 → 画面に紐付け Building ID 表示
6. 30 秒ごとに `ping` で生存確認
7. ボタン B でテスト `echo` 送信 (実機検証用)
8. ボタン A 長押し (3 秒) で全設定リセット + 再起動

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 起動後 AP "Stack-chan-Setup" が見えない | M5Stack を再起動。それでも出ない場合はファーム書き込み確認 |
| AP 接続後ブラウザが設定 UI を開かない | ブラウザで `192.168.4.1` を直接開く |
| Wi-Fi 接続後 `WebSocket disconnected` | Server URL のフォーマット確認 (`ws://host:port/path`)、SAIVerse が起動中か確認 |
| `error: auth_failed` | Vessel ID / Device Token の入力ミス、または SAIVerse 側でペアリング解除済み |
| 設定を間違えた | 起動中にボタン A を 3 秒長押し → 全設定リセット → 再ペアリング |

## ライセンス

Apache License 2.0 (Stack-chan エコシステムと整合)。

依存ライブラリのライセンス: `../NOTICE` を参照。
