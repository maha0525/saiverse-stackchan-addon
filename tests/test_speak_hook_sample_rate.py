"""機体へ送る声のサンプルレート (speak_hook) のテスト。

2026-10-05、speak_hook は voice-tts の音声を、エンジンに関係なく常に
32 kHz (GPT-SoVITS の出力) として gateway へ送っていた。OpenAI TTS と
ElevenLabs は 24 kHz で音を出すので、機体では 32/24 倍の速さと高さで
再生されるはずだった。発話ごとに voice-tts が開いた PCM stream の
サンプルレートを読んで送ることを確かめる。
"""
import importlib.util
import queue
import sys
from pathlib import Path

import pytest

_ADDON_DIR = Path(__file__).resolve().parents[1]
if str(_ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(_ADDON_DIR))


def _load_speak_hook():
    name = "speak_hook"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ADDON_DIR / "speak_hook.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    status_code = 200
    text = "ok"


def _run_post(monkeypatch, speak_hook, get_pcm_stream_info):
    """voice-tts の PCM stream を 1 発話ぶん流し、gateway へ送ったヘッダを返す。"""
    pcm_queue: "queue.Queue" = queue.Queue()
    pcm_queue.put(b"\x01\x00" * 160)
    pcm_queue.put(None)

    def subscribe_pcm(message_id):
        return pcm_queue

    monkeypatch.setattr(
        speak_hook,
        "_load_voice_tts_subscribe",
        lambda: (subscribe_pcm, get_pcm_stream_info),
    )

    sent = {}

    def fake_post(url, data, headers, timeout):
        sent["headers"] = headers
        sent["body"] = b"".join(data)
        return _FakeResponse()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    state = speak_hook._ActivePostState(message_id="msg-1", pulse_id="p-1")
    speak_hook._post_pcm_in_background(
        "msg-1",
        "",
        "http://127.0.0.1:8766/pcm",
        speak_hook._VOICE_TTS_SAMPLE_RATE,
        "vessel-1",
        state,
        None,
    )
    assert state.completed.is_set()
    return sent["headers"]


def test_sends_the_engines_sample_rate(monkeypatch):
    speak_hook = _load_speak_hook()
    headers = _run_post(monkeypatch, speak_hook, lambda message_id: (24000, 1))
    assert headers["X-Sample-Rate"] == "24000"


def test_falls_back_to_default_when_stream_info_is_missing(monkeypatch):
    speak_hook = _load_speak_hook()
    headers = _run_post(monkeypatch, speak_hook, lambda message_id: None)
    assert headers["X-Sample-Rate"] == "32000"


def test_falls_back_to_default_for_old_voice_tts(monkeypatch):
    speak_hook = _load_speak_hook()
    headers = _run_post(monkeypatch, speak_hook, None)
    assert headers["X-Sample-Rate"] == "32000"


@pytest.mark.parametrize("info", [(0, 1), RuntimeError("boom")])
def test_invalid_stream_info_falls_back(monkeypatch, info):
    speak_hook = _load_speak_hook()

    def get_pcm_stream_info(message_id):
        if isinstance(info, Exception):
            raise info
        return info

    headers = _run_post(monkeypatch, speak_hook, get_pcm_stream_info)
    assert headers["X-Sample-Rate"] == "32000"
