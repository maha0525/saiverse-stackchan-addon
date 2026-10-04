"""機体の声を SAIVerse へ届ける受け口 (audio_input_relay) のテスト。

2026-10-04、画面で別の Building を開いている間は、機体に話しかけた声が
本体の現在地の照合 (「ユーザーは自分がいる Building にだけ発言できる」) で
断られ、ペルソナが返事をしなかった。画面からの発言 (/chat/utter) と同じく、
先にユーザーを Vessel Building へ移してから届けることを確かめる。
"""
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ADDON_DIR = Path(__file__).resolve().parents[1]
if str(_ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(_ADDON_DIR))


def _load_relay():
    name = "audio_input_relay"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ADDON_DIR / "audio_input_relay.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VESSEL_BUILDING = "stackchan_room"
OGG = b"OggS" + b"\x00" * 60


class _FakeVesselManager:
    def __init__(self):
        self.vessel = SimpleNamespace(
            vessel_id="v1",
            bound_building_id=VESSEL_BUILDING,
            bound_persona_id=None,
        )

    def verify_token(self, token):
        return self.vessel if token == "good" else None

    def get_vessel(self, vessel_id):
        return self.vessel if vessel_id == "v1" else None

    def update_last_seen(self, vessel_id):
        pass


class _FakeManager:
    def __init__(self, current, move_result=(True, "moved")):
        self.state = SimpleNamespace(user_current_building_id=current)
        self.move_result = move_result
        self.moves = []
        self.inputs = []

    def move_user(self, target):
        self.moves.append(target)
        ok, msg = self.move_result
        if ok and not getattr(msg, "code", None):
            self.state.user_current_building_id = target
        return ok, msg

    def handle_user_input_stream(self, message, metadata=None, building_id=None):
        # 本体と同じ順序: 呼ばれた時点の現在地で照合する
        self.inputs.append((building_id, self.state.user_current_building_id))
        return iter(())


@pytest.fixture
def relay(tmp_path, monkeypatch):
    module = _load_relay()
    vm = _FakeVesselManager()
    monkeypatch.setattr(module, "get_vessel_manager", lambda: vm)
    monkeypatch.setattr(
        module, "_save_ogg_capture",
        lambda body: (tmp_path / "x.ogg", "x.ogg"),
    )
    return module


def _post(module, manager, monkeypatch):
    monkeypatch.setattr(module, "get_manager", lambda: manager)
    app = FastAPI()
    app.include_router(module.audio_router)
    client = TestClient(app)
    return client.post(
        "/audio-in?vessel=v1",
        content=OGG,
        headers={"Authorization": "Bearer good", "Content-Type": "audio/ogg"},
    )


def test_moves_user_into_vessel_building_before_delivering(relay, monkeypatch):
    manager = _FakeManager(current="eris_room")
    res = _post(relay, manager, monkeypatch)

    assert res.status_code == 200, res.text
    assert manager.moves == [VESSEL_BUILDING]
    # 届けた時点で、ユーザーはもう Vessel Building にいる
    assert manager.inputs == [(VESSEL_BUILDING, VESSEL_BUILDING)]


def test_does_not_move_when_already_there(relay, monkeypatch):
    manager = _FakeManager(current=VESSEL_BUILDING)
    res = _post(relay, manager, monkeypatch)

    assert res.status_code == 200, res.text
    assert manager.moves == []
    assert manager.inputs == [(VESSEL_BUILDING, VESSEL_BUILDING)]


def test_does_not_deliver_when_move_fails(relay, monkeypatch):
    manager = _FakeManager(current="eris_room", move_result=(False, "full"))
    res = _post(relay, manager, monkeypatch)

    assert res.status_code == 409
    assert manager.inputs == []


def test_does_not_deliver_when_move_stops_at_entrance(relay, monkeypatch):
    from saiverse.occupancy_manager import REDIRECTED_TO_ENTRANCE

    notice = SimpleNamespace(code=REDIRECTED_TO_ENTRANCE, current_building_id="gate")
    manager = _FakeManager(current="eris_room", move_result=(True, notice))
    res = _post(relay, manager, monkeypatch)

    assert res.status_code == 409
    assert manager.inputs == []


def test_drain_logs_a_refusal_instead_of_dropping_it(relay, caplog):
    refusal = '{"type": "error", "error_code": "not_in_building"}\n'
    with caplog.at_level(logging.WARNING):
        relay._drain_stream_sync(iter([refusal]))
    assert "refused the device voice" in caplog.text
    assert "not_in_building" in caplog.text


def test_async_handler_is_coroutine(relay):
    # receive_device_audio が移動を asyncio.to_thread で待つ前提 (イベント
    # ループを塞がない) を壊していないことの確認
    assert asyncio.iscoroutinefunction(relay.receive_device_audio)
