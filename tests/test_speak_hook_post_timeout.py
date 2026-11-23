"""gateway への PCM POST が、送信の詰まりで切れないことのテスト。

2026-11-23、ElevenLabs で 5 秒を超える発話が、約 5.0 秒で
``('Connection aborted.', TimeoutError('timed out'))`` になって途中で切れる
という報告があった。speak_hook は ``timeout=(5, 300)`` を渡していたが、
requests が使う urllib3 は body を送っている間も一つ目 (connect) の 5 秒を
ソケットの待ち時間に使う。gateway は音を再生の速さでしか読まないので、
再生より速く音を作るエンジンでは送り出しが詰まり、5 秒で切れていた。

ここでは本物のソケットで、読むのを止めている受け手に大きな body を送り、
(1) connect の値が送信中の待ち時間を決めていること (urllib3 の挙動)、
(2) speak_hook が渡す値なら、その詰まりで切れないこと、を確かめる。
"""
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

_ADDON_DIR = Path(__file__).resolve().parents[1]
if str(_ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(_ADDON_DIR))

# 受け手が読むのを止めている時間。送信の待ち時間 (下の _SHORT) より長くする。
_STALL_SECONDS = 1.5
_SHORT = 0.5
# ソケットの送受信バッファを確実に溢れさせる大きさ。
_BODY_BYTES = 32 * 1024 * 1024


def _load_speak_hook():
    name = "speak_hook"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ADDON_DIR / "speak_hook.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _start_stalling_server():
    """接続を受けたあと _STALL_SECONDS のあいだ読まず、そのあと全部読んで 200 を返す。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        try:
            conn.settimeout(10)
            time.sleep(_STALL_SECONDS)
            data = b""
            while b"0\r\n\r\n" not in data[-16:]:
                chunk = conn.recv(1024 * 1024)
                if not chunk:
                    break
                data = data[-16:] + chunk
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
        except OSError:
            pass
        finally:
            conn.close()
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


def _body():
    # speak_hook と同じく iterator を渡して chunked で送る。
    piece = b"\x00" * (256 * 1024)
    for _ in range(_BODY_BYTES // len(piece)):
        yield piece


def test_connect_timeout_governs_the_upload():
    """urllib3 の挙動: 送信中の詰まりが connect の値を超えると切れる。"""
    port, thread = _start_stalling_server()
    with pytest.raises(requests.RequestException):
        requests.post(
            f"http://127.0.0.1:{port}/pcm", data=_body(), timeout=(_SHORT, 30)
        )
    thread.join(timeout=15)


def test_speak_hook_timeout_survives_a_stalled_upload():
    speak_hook = _load_speak_hook()
    connect, read = speak_hook._PCM_POST_TIMEOUT
    # 発話の最大長 (5 分) の詰まりに耐える値であること。
    assert connect >= 300 and read >= 300

    port, thread = _start_stalling_server()
    resp = requests.post(
        f"http://127.0.0.1:{port}/pcm",
        data=_body(),
        timeout=speak_hook._PCM_POST_TIMEOUT,
    )
    assert resp.status_code == 200
    thread.join(timeout=15)
