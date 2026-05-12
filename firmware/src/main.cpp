/**
 * SAIVerse Stack-chan Vessel Firmware (Phase 1)
 *
 * Target: M5Stack CoreS3 (ESP32-S3, StackChan AI Desktop Robot)
 *
 * フロー:
 *   1. 起動 → M5Unified 初期化
 *   2. Preferences から保存設定読込 (server_url / vessel_id / device_token)
 *   3. WiFiManager で Wi-Fi 接続 (初回は AP モード "Stack-chan-Setup" 起動)
 *      AP モードの Web UI で以下を入力:
 *        - Wi-Fi SSID / Password
 *        - SAIVerse Server URL (例: ws://192.168.1.10:3000/api/addon/saiverse-stackchan-addon/vessel)
 *        - Vessel ID (UUID)
 *        - Device Token
 *   4. Wi-Fi 接続後 WebSocket 接続 → hello 送信
 *   5. welcome 受信で認証完了、画面に bound_building_id 表示
 *   6. 30 秒ごとに ping、ボタン B でテスト echo 送信
 *   7. ボタン A 長押し (3 秒) で設定リセット + 再起動
 *
 * 詳細プロトコル: docs/intent/stackchan_vessel.md (SAIVerse 本体側)
 */
#include <Arduino.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

// ============================================================================
// 設定 / 状態
// ============================================================================
static const char *PREF_NAMESPACE = "stackchan";
static const char *AP_NAME = "Stack-chan-Setup";
static const unsigned long PING_INTERVAL_MS = 30000;
static const unsigned long RECONNECT_INTERVAL_MS = 5000;
static const String FIRMWARE_VERSION = "0.2.0";

Preferences prefs;
WebSocketsClient webSocket;

String serverUrl;
String vesselId;
String deviceToken;

bool isAuthenticated = false;
unsigned long lastPingMs = 0;
uint32_t pingSeq = 0;

// ============================================================================
// Phase 2 (PCM 直送版): ストリーミング再生
// ============================================================================
// voice-tts は PCM (signed 16-bit little-endian) を audio_stream_bridge 経由で
// WS binary frame として送ってくる。本ファームは MP3 decode を介さず、PCM を
// 直接 M5.Speaker.playRaw に渡す。
//
// 旧設計 (MP3 + libhelix) は frame sync 失敗で「途中で切れて次が始まる」
// 問題があったため廃止。voice-tts 本体の sounddevice 経路と同じ「PCM を直接
// 再生する」アプローチに合わせた。
//
// スレッド設計:
//   - WStype_BIN ハンドラ (Core 1 / Arduino loop): ringbuf に push するだけ
//   - audioPlaybackTask (Core 0): ringbuf から PCM を取り出して playRaw に渡す
//
// playRaw の制約:
//   M5.Speaker.playRaw は data を内部コピーせず ポインタだけ保存する
//   (Speaker_Class.cpp:1029、hpp の @attention 明示)。受信した PCM を直接
//   渡すと、ringbuf の循環で同じアドレスが上書きされて壊れる。
//   → 自前の rotation buffer (4 個) にコピーしてから渡す。
//   M5.Speaker は 1 channel あたり slot=2 (current + next) なので 4 buffer
//   で余裕。

static bool audioPlaying = false;
static RingbufHandle_t audioRingBuf = nullptr;
static TaskHandle_t audioTaskHandle = nullptr;
static constexpr size_t AUDIO_RINGBUF_SIZE = 32 * 1024;  // 32 KB

// audio_start で受信した PCM フォーマット (デフォルト: 32 kHz mono)。
// voice-tts は GPT-SoVITS の出力に合わせて sample_rate を変える可能性が
// あるので、毎回 audio_start で更新する。
static volatile uint32_t currentSampleRate = 32000;
static volatile uint8_t currentChannels = 1;

// PCM rotation buffer (ringbuf → M5.Speaker.playRaw 受け渡し用)。
// 1 buffer = 16384 samples = 32 KB (16-bit mono)。
//
// 重要: xRingbufferReceive (BYTEBUF) は **現時点で連続して取れる byte 列を
// すべて** 返す挙動。bridge が 8 KB chunk を送っても、ringbuf に複数連続して
// 溜まっている時は 16 KB / 22 KB / 32 KB 等まとめて取れる。1 buffer のサイズが
// 小さいと超過分を truncate するしかなく、音が途切れる原因になる。
// よって ringbuf 全体 (32 KB) と同じサイズに揃える。
//
// メモリ: 4 buffer × 32 KB = 128 KB (ESP32-S3 DRAM 512 KB のうち 25%)。
// M5.Speaker は 1 channel あたり slot=2 (current + next) で、3 つ目以降は
// wait する。4 buffer rotation で wait による pace と矛盾せず動く。
static constexpr size_t PCM_ROT_COUNT = 4;
static constexpr size_t PCM_ROT_MAX_SAMPLES = 16384;  // = 32 KB
static int16_t pcmRotBuf[PCM_ROT_COUNT][PCM_ROT_MAX_SAMPLES];
static size_t pcmRotIdx = 0;

// Core 0 で動くオーディオ処理タスク。ringbuf から PCM bytes を取り出して
// playRaw に流す。
static void audioPlaybackTask(void * /*param*/) {
    while (true) {
        if (!audioPlaying || audioRingBuf == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        size_t itemSize = 0;
        uint8_t *data = (uint8_t *)xRingbufferReceive(
            audioRingBuf, &itemSize, pdMS_TO_TICKS(50)
        );
        if (data != nullptr && itemSize > 0) {
            // PCM 16-bit、sample 数 = byte 数 / 2
            size_t sampleCount = itemSize / sizeof(int16_t);
            if (sampleCount > PCM_ROT_MAX_SAMPLES) {
                // 想定 (8KB chunk = 4096 samples) を超えた場合の保護
                Serial.printf(
                    "[audio] chunk too large: %u samples (max=%u), truncate\n",
                    (unsigned)sampleCount, (unsigned)PCM_ROT_MAX_SAMPLES
                );
                sampleCount = PCM_ROT_MAX_SAMPLES;
            }
            int16_t *dst = pcmRotBuf[pcmRotIdx];
            memcpy(dst, data, sampleCount * sizeof(int16_t));
            pcmRotIdx = (pcmRotIdx + 1) % PCM_ROT_COUNT;

            // M5.Speaker.playRaw シグネチャ:
            //   playRaw(data, array_len, sample_rate, stereo, repeat=1, channel=-1, stop=false)
            // - array_len は int16_t 個数 (sample 数)
            // - channel=0 固定で連続再生 (slot=2 の wait で自動 pace)
            M5.Speaker.playRaw(
                dst, sampleCount, currentSampleRate,
                currentChannels == 2, 1, 0
            );
            vRingbufferReturnItem(audioRingBuf, (void *)data);
        }
    }
}

// ============================================================================
// 画面表示ヘルパ
// ============================================================================
static void displayStatus(const String &line1,
                          const String &line2 = "",
                          const String &line3 = "",
                          const String &line4 = "") {
    M5.Display.clear();
    M5.Display.setCursor(0, 0);
    M5.Display.setTextSize(2);
    M5.Display.println("Stack-chan Vessel");
    M5.Display.setTextSize(1);
    M5.Display.printf("fw %s\n\n", FIRMWARE_VERSION.c_str());
    if (!line1.isEmpty()) M5.Display.println(line1);
    if (!line2.isEmpty()) M5.Display.println(line2);
    if (!line3.isEmpty()) M5.Display.println(line3);
    if (!line4.isEmpty()) M5.Display.println(line4);
}

// ============================================================================
// プロトコル: メッセージ送信
// ============================================================================
static void sendJson(JsonDocument &doc, const char *tag) {
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
    Serial.printf("[ws] -> %s: %s\n", tag, out.c_str());
}

static void sendHello() {
    JsonDocument doc;
    doc["type"] = "hello";
    doc["vessel_id"] = vesselId;
    doc["device_token"] = deviceToken;
    doc["firmware_version"] = FIRMWARE_VERSION;
    JsonArray caps = doc["capabilities"].to<JsonArray>();
    caps.add("text_echo");
    sendJson(doc, "hello");
}

static void sendPing() {
    pingSeq++;
    JsonDocument doc;
    doc["type"] = "ping";
    doc["seq"] = pingSeq;
    sendJson(doc, "ping");
}

static void sendEcho(const String &text) {
    JsonDocument doc;
    doc["type"] = "echo";
    doc["text"] = text;
    sendJson(doc, "echo");
}

// ============================================================================
// プロトコル: メッセージ受信
// ============================================================================
static void onWsMessage(uint8_t *payload, size_t length) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload, length);
    if (err) {
        Serial.printf("[ws] invalid JSON: %s\n", err.c_str());
        return;
    }
    const char *type = doc["type"];
    if (!type) {
        Serial.println("[ws] message has no 'type'");
        return;
    }

    if (strcmp(type, "welcome") == 0) {
        isAuthenticated = true;
        const char *buildingId = doc["bound_building_id"] | "(unknown)";
        Serial.printf("[ws] <- welcome building=%s\n", buildingId);
        displayStatus("Status: connected",
                      "building:",
                      String(buildingId),
                      "Touch screen 5s = reset");
    } else if (strcmp(type, "pong") == 0) {
        uint32_t seq = doc["seq"] | 0;
        Serial.printf("[ws] <- pong seq=%u\n", seq);
    } else if (strcmp(type, "echo_reply") == 0) {
        const char *text = doc["text"] | "";
        Serial.printf("[ws] <- echo_reply: %s\n", text);
        displayStatus("Echo reply:", String(text));
    } else if (strcmp(type, "audio_start") == 0) {
        // Phase 2 (PCM 直送): 発話開始通知 + PCM フォーマット
        // 期待 JSON: {type, message_id, sample_rate, channels, format="pcm_s16le"}
        const char *msgId = doc["message_id"] | "?";
        uint32_t sr = doc["sample_rate"] | 32000;
        uint8_t ch = doc["channels"] | 1;
        const char *fmt = doc["format"] | "pcm_s16le";

        // 割り込み再生: 既に再生中なら前の発話を中断する
        // (1) audioPlaying=false で audio task を一時停止
        // (2) ring buffer を空にする
        // (3) M5.Speaker.stop で I2S DMA の再生中音を停止
        // この間に新 sample_rate / channels を設定してから audioPlaying=true で再開
        if (audioPlaying) {
            Serial.println("[audio] interrupting current playback");
            audioPlaying = false;
            // audio task が次の iter で wait に入るまで少し待つ
            // (xRingbufferReceive 50ms timeout 内に入っている可能性)
            delay(5);
            // ring buffer drain
            if (audioRingBuf != nullptr) {
                size_t itemSize = 0;
                while (true) {
                    uint8_t *data = (uint8_t *)xRingbufferReceive(
                        audioRingBuf, &itemSize, 0  // no wait
                    );
                    if (data == nullptr) break;
                    vRingbufferReturnItem(audioRingBuf, (void *)data);
                }
            }
            // I2S DMA の再生中バッファを停止 (channel=0 のみ)
            M5.Speaker.stop(0);
        }

        currentSampleRate = sr;
        currentChannels = ch;
        Serial.printf("[ws] <- audio_start msg=%s sr=%u ch=%u fmt=%s\n",
                      msgId, (unsigned)sr, (unsigned)ch, fmt);
        audioPlaying = true;
        displayStatus("Speaking...", String("msg: ") + String(msgId).substring(0, 12));
    } else if (strcmp(type, "audio_end") == 0) {
        // Phase 2 (PCM 直送): 発話終了通知 → アイドル状態へ
        const char *msgId = doc["message_id"] | "?";
        Serial.printf("[ws] <- audio_end msg=%s\n", msgId);
        audioPlaying = false;
    } else if (strcmp(type, "error") == 0) {
        const char *code = doc["code"] | "?";
        const char *reason = doc["reason"] | "?";
        Serial.printf("[ws] <- error code=%s reason=%s\n", code, reason);
        displayStatus("Error", String("code: ") + code, String(reason));
        isAuthenticated = false;
    } else {
        Serial.printf("[ws] <- unknown type: %s\n", type);
    }
}

// ============================================================================
// WebSocket イベントハンドラ
// ============================================================================
static void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
            Serial.println("[ws] disconnected");
            isAuthenticated = false;
            displayStatus("Disconnected", "Reconnecting...");
            break;
        case WStype_CONNECTED:
            Serial.printf("[ws] connected: %s\n", (char *)payload);
            displayStatus("Connected", "Sending hello...");
            sendHello();
            break;
        case WStype_TEXT:
            onWsMessage(payload, length);
            break;
        case WStype_BIN:
            // Phase 2 (PCM 直送): PCM 16-bit chunk (audio_start 後、audio_end までの binary)
            // WS ハンドラはメインタスクで動くため、ここで playRaw まで呼ぶと
            // WebSocketsClient.loop() が回らなくなる。ring buffer に push する
            // だけに留め、Core 0 の audioPlaybackTask が playRaw に渡す。
            if (audioPlaying && audioRingBuf != nullptr) {
                if (xRingbufferSend(audioRingBuf, payload, length,
                                    pdMS_TO_TICKS(50)) != pdTRUE) {
                    Serial.printf(
                        "[ws] ring buffer send timeout (%u bytes dropped)\n",
                        (unsigned)length
                    );
                }
            }
            break;
        case WStype_ERROR:
            Serial.printf("[ws] error: %.*s\n", (int)length, (char *)payload);
            break;
        default:
            break;
    }
}

// ============================================================================
// WebSocket 接続セットアップ
// ============================================================================
static bool setupWebSocket() {
    // serverUrl 例:
    //   ws://192.168.1.10:8000/api/addon/saiverse-stackchan-addon/vessel
    //   wss://saiverse.local/api/addon/saiverse-stackchan-addon/vessel
    String url = serverUrl;
    bool isSecure = false;

    if (url.startsWith("wss://")) {
        isSecure = true;
        url = url.substring(6);
    } else if (url.startsWith("ws://")) {
        url = url.substring(5);
    } else if (url.startsWith("http://")) {
        url = url.substring(7);
    } else if (url.startsWith("https://")) {
        isSecure = true;
        url = url.substring(8);
    }

    int slashIdx = url.indexOf('/');
    String hostPort = (slashIdx > 0) ? url.substring(0, slashIdx) : url;
    String path = (slashIdx > 0) ? url.substring(slashIdx) : "/";

    int colonIdx = hostPort.indexOf(':');
    String host = (colonIdx > 0) ? hostPort.substring(0, colonIdx) : hostPort;
    uint16_t port = (colonIdx > 0)
                        ? (uint16_t)hostPort.substring(colonIdx + 1).toInt()
                        : (isSecure ? 443 : 80);

    if (host.isEmpty()) {
        Serial.println("[ws] invalid server URL, halting");
        displayStatus("Invalid URL", serverUrl);
        return false;
    }

    Serial.printf("[ws] connecting %s:%u%s (secure=%d)\n",
                  host.c_str(), port, path.c_str(), isSecure);

    if (isSecure) {
        webSocket.beginSSL(host.c_str(), port, path.c_str());
    } else {
        webSocket.begin(host.c_str(), port, path.c_str());
    }
    webSocket.onEvent(onWsEvent);
    webSocket.setReconnectInterval(RECONNECT_INTERVAL_MS);
    // WebSocket protocol-level ping/pong (binary opcode 0x9 / 0xA、JSON の
    // sendPing とは別系統)。library 自身が定期的に ping を送り、pong を待つ。
    // アイドル時の TCP 半生半死状態を library レベルで検知して disconnect →
    // setReconnectInterval により自動再接続が走るようにする。
    //
    // 引数: ping_interval (15s) / pong_timeout (3s) / disconnect_timeout_count (2)
    // = 15 秒ごとに ping、3 秒以内に pong 返らなければ失敗、2 回連続失敗で切断
    webSocket.enableHeartbeat(15000, 3000, 2);
    return true;
}

// ============================================================================
// Wi-Fi 接続セットアップ (WiFiManager で AP モード or 自動接続)
// ============================================================================
static void setupWiFi() {
    WiFiManager wm;
    // SAIVerse 固有のカスタムパラメータ
    WiFiManagerParameter customServer("server",
                                      "SAIVerse Server URL (ws://host:port/api/addon/...)",
                                      serverUrl.c_str(), 256);
    WiFiManagerParameter customVessel("vessel", "Vessel ID (UUID)",
                                      vesselId.c_str(), 64);
    WiFiManagerParameter customToken("token", "Device Token",
                                     deviceToken.c_str(), 128);
    wm.addParameter(&customServer);
    wm.addParameter(&customVessel);
    wm.addParameter(&customToken);

    // 初回起動 (Preferences が空) かどうか判定。
    // 一つでも欠けていたら設定不完全とみなして初回扱いにする。
    bool isFirstSetup =
        serverUrl.isEmpty() || vesselId.isEmpty() || deviceToken.isEmpty();

    if (isFirstSetup) {
        // WiFiManager v2.0.17 の autoConnect は ESP32 で `wifiIsSaved = true`
        // を hardcoded workaround として持っており、保存設定が無くても
        // connectWifi() で接続試行に入り、タイムアウト待ち (数十秒〜数分) に
        // なってから AP モードを起動する。これだと初回セットアップの体験が
        // ひどく悪いため、初回は startConfigPortal を直接呼んで AP モードを
        // 即起動する (autoConnect の前段スキップ)。
        Serial.println("[wifi] first setup -> starting AP mode immediately");
        displayStatus("WiFi setup", "AP: " + String(AP_NAME),
                      "Connect from phone",
                      "192.168.4.1 in browser");

        // 念のため SDK 内部の WiFi credentials も erase しておく
        // (M5Stack 出荷時ファームが何か書いていた場合の保険)。
        WiFi.disconnect(true, true);
        delay(100);

        bool configured = wm.startConfigPortal(AP_NAME);
        if (!configured) {
            Serial.println("[wifi] config portal exited without save, restart");
            displayStatus("Setup cancelled", "Restarting in 3s");
            delay(3000);
            ESP.restart();
        }
    } else {
        // 設定済み: 自動接続。
        Serial.println("[wifi] saved config present, attempting autoConnect");
        displayStatus("WiFi connecting...",
                      "(fallback AP: " + String(AP_NAME) + ")");
        bool connected = wm.autoConnect(AP_NAME);
        if (!connected) {
            Serial.println("[wifi] autoConnect failed, restarting");
            displayStatus("WiFi failed", "Restarting in 3s");
            delay(3000);
            ESP.restart();
        }
    }

    // 設定 UI で入力された値を Preferences に永続化
    serverUrl = String(customServer.getValue());
    vesselId = String(customVessel.getValue());
    deviceToken = String(customToken.getValue());
    prefs.putString("server_url", serverUrl);
    prefs.putString("vessel_id", vesselId);
    prefs.putString("device_token", deviceToken);

    Serial.printf("[wifi] connected SSID=%s IP=%s\n",
                  WiFi.SSID().c_str(),
                  WiFi.localIP().toString().c_str());
    displayStatus("WiFi: " + WiFi.SSID(),
                  "IP: " + WiFi.localIP().toString());
}

// ============================================================================
// Arduino entry points
// ============================================================================
void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);
    M5.Display.setBrightness(80);
    M5.Display.setTextWrap(true);

    // Phase 2: M5.Speaker を有効化 (TTS ストリーミング再生用)
    M5.Speaker.begin();
    M5.Speaker.setVolume(200);  // 0-255、200 でほぼ最大

    // Phase 2: オーディオ処理を Core 0 の別タスクに分離 (詳細はファイル上部の
    // コメント参照)。リングバッファに WS 受信を入れて、別タスクでデコード/再生。
    audioRingBuf = xRingbufferCreate(AUDIO_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    if (audioRingBuf == nullptr) {
        Serial.println("[setup] failed to create audio ring buffer!");
    } else {
        xTaskCreatePinnedToCore(
            audioPlaybackTask,
            "audio_playback",
            8192,        // stack size (bytes)
            nullptr,     // task parameter
            1,           // priority (低、main loop と同じ程度)
            &audioTaskHandle,
            0            // Core 0 (Arduino main loop は Core 1)
        );
        Serial.println("[setup] audio_playback task started on core 0");
    }

    Serial.begin(115200);
    delay(200);
    Serial.println();
    Serial.println("=== SAIVerse Stack-chan Vessel firmware ===");
    Serial.printf("Firmware: %s\n", FIRMWARE_VERSION.c_str());

    displayStatus("Booting...");

    prefs.begin(PREF_NAMESPACE, false);
    serverUrl = prefs.getString("server_url", "");
    vesselId = prefs.getString("vessel_id", "");
    deviceToken = prefs.getString("device_token", "");
    Serial.printf("[prefs] loaded server=%s vessel=%s token=%s\n",
                  serverUrl.c_str(),
                  vesselId.c_str(),
                  deviceToken.isEmpty() ? "(empty)" : "(set)");

    setupWiFi();

    if (serverUrl.isEmpty() || vesselId.isEmpty() || deviceToken.isEmpty()) {
        // ここに来るのは startConfigPortal が完了した直後で値が空のままだった
        // ケース (ユーザーが入力せず終了した場合等)。通常フローでは到達しない。
        Serial.println("[setup] missing config after setupWiFi, halting");
        displayStatus("Config missing",
                      "Touch screen 5s",
                      "to reset & retry");
        unsigned long touchStartMs = 0;
        while (true) {
            M5.update();
            auto t = M5.Touch.getDetail();
            if (t.isPressed()) {
                if (touchStartMs == 0) touchStartMs = millis();
                else if (millis() - touchStartMs > 5000) {
                    prefs.clear();
                    WiFi.disconnect(true, true);
                    ESP.restart();
                }
            } else {
                touchStartMs = 0;
            }
            delay(50);
        }
    }

    if (!setupWebSocket()) {
        Serial.println("[setup] websocket init failed, halting");
        while (true) {
            delay(1000);
        }
    }
}

void loop() {
    M5.update();
    webSocket.loop();

    // 認証完了後、定期 ping で生存確認
    if (isAuthenticated && (millis() - lastPingMs > PING_INTERVAL_MS)) {
        sendPing();
        lastPingMs = millis();
    }

    // 10 秒ごとに状態 dump (disconnect 前後の状況を後から追跡できるよう)
    static unsigned long lastDebugLogMs = 0;
    constexpr unsigned long DEBUG_LOG_INTERVAL_MS = 10000;
    if (millis() - lastDebugLogMs > DEBUG_LOG_INTERVAL_MS) {
        Serial.printf(
            "[debug] uptime=%lus auth=%d audio=%d ws_conn=%d "
            "ringbuf_free=%u heap_free=%u\n",
            millis() / 1000,
            (int)isAuthenticated,
            (int)audioPlaying,
            (int)webSocket.isConnected(),
            audioRingBuf ? (unsigned)xRingbufferGetCurFreeSize(audioRingBuf) : 0u,
            (unsigned)ESP.getFreeHeap()
        );
        lastDebugLogMs = millis();
    }

    // 設定リセット: 画面長押し (5 秒)
    //
    // CoreS3 には Core / Core2 のような物理ボタン A/B/C が無く、M5Unified の
    // M5.config() デフォルトでは touch button (BtnA/B/C) も有効化されない。
    // そのため Touch API で画面のどこを押されたかを直接見る。
    // (Phase 5 で touch を「なでなで」入力として使う際は、リセット用の長押し
    // 閾値とユーザータッチを区別する設計に拡張する)
    static unsigned long touchStartMs = 0;
    auto t = M5.Touch.getDetail();
    if (t.isPressed()) {
        if (touchStartMs == 0) {
            touchStartMs = millis();
        } else if (millis() - touchStartMs > 5000) {
            Serial.println("[setup] config reset triggered by 5s touch");
            displayStatus("Resetting...", "All config will be cleared");
            prefs.clear();
            WiFi.disconnect(true, true);
            delay(2000);
            ESP.restart();
        }
    } else {
        touchStartMs = 0;
    }

    delay(5);
}
