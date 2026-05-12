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

// ============================================================================
// 設定 / 状態
// ============================================================================
static const char *PREF_NAMESPACE = "stackchan";
static const char *AP_NAME = "Stack-chan-Setup";
static const unsigned long PING_INTERVAL_MS = 30000;
static const unsigned long RECONNECT_INTERVAL_MS = 5000;
static const String FIRMWARE_VERSION = "0.1.0";

Preferences prefs;
WebSocketsClient webSocket;

String serverUrl;
String vesselId;
String deviceToken;

bool isAuthenticated = false;
unsigned long lastPingMs = 0;
uint32_t pingSeq = 0;

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
                      "BtnB=echo  BtnA(hold)=reset");
    } else if (strcmp(type, "pong") == 0) {
        uint32_t seq = doc["seq"] | 0;
        Serial.printf("[ws] <- pong seq=%u\n", seq);
    } else if (strcmp(type, "echo_reply") == 0) {
        const char *text = doc["text"] | "";
        Serial.printf("[ws] <- echo_reply: %s\n", text);
        displayStatus("Echo reply:", String(text));
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

    displayStatus("WiFi setup", "AP: " + String(AP_NAME),
                  "(if not configured)");

    bool connected = wm.autoConnect(AP_NAME);
    if (!connected) {
        Serial.println("[wifi] failed to connect, restarting...");
        displayStatus("WiFi failed", "Restarting...");
        delay(3000);
        ESP.restart();
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
        Serial.println("[setup] missing config, halting (hold BtnA 3s to reset)");
        displayStatus("Config missing",
                      "Hold BtnA 3s",
                      "to reset & retry");
        while (true) {
            M5.update();
            if (M5.BtnA.pressedFor(3000)) {
                prefs.clear();
                WiFi.disconnect(true, true);
                ESP.restart();
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

    // 設定リセット: ボタン A 長押し
    if (M5.BtnA.pressedFor(3000)) {
        Serial.println("[setup] config reset triggered");
        displayStatus("Resetting...", "All config will be cleared");
        prefs.clear();
        WiFi.disconnect(true, true);
        delay(2000);
        ESP.restart();
    }

    // ボタン B: テスト echo 送信 (実機検証用)
    if (M5.BtnB.wasPressed() && isAuthenticated) {
        sendEcho("hello from device");
    }

    delay(5);
}
