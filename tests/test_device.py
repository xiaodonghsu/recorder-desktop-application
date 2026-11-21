import asyncio
from types import SimpleNamespace

import pytest

from voicehub.capture import packets
from voicehub.device import Recorder
from voicehub.protocol import KEY, MAIN, Parser, encode


class FakeClient:
    auth_reply = b"\0"
    serial_number = b"G1000897"

    def __init__(self, device, disconnected_callback):
        self.callback = disconnected_callback
        self.is_connected = False
        self.notify = {}
        self.writes = []
        self.services = SimpleNamespace(
            get_service=lambda _: object(),
            get_characteristic=lambda _: SimpleNamespace(
                properties=["write-without-response"], max_write_without_response_size=244
            ),
        )

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.callback(self)

    async def start_notify(self, uuid, callback):
        self.notify[uuid] = callback

    def emit(self, kind, cmd, body=b""):
        self.notify[MAIN](None, encode(199, kind, cmd, body))

    async def write_gatt_char(self, char, data, response):
        frame = Parser().feed(data)[0]
        self.writes.append(frame)
        replies = {
            (0, 50): (51, self.auth_reply),
            (0, 3): (4, b"\x64"),
            (3, 25): (26, b"\x02"),
            (0, 1): (2, b"\x01\0\0\0\x02\0\0\0"),
            (0, 10): (11, b"v1\0"),
            (0, 12): (13, self.serial_number),
        }
        if (frame.kind, frame.cmd) in replies:
            cmd, body = replies[frame.kind, frame.cmd]
            self.emit(frame.kind, cmd, body)
        elif (frame.kind, frame.cmd) == (1, 0):
            self.emit(1, 0, b"live.opus")
            self.emit(1, 1, b"\x48" + bytes(39))
        elif (frame.kind, frame.cmd) == (1, 2):
            self.emit(1, 1, b"\x48" + bytes(39))  # audio arriving during stop is retained
            self.emit(1, 4, b"\x02")


async def test_auth_info_and_capture(tmp_path):
    events = []
    recorder = Recorder(lambda *e: events.append(e), FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10 test"), "ab" * 32)
    assert recorder.authorized
    info = next(v for e, v in events if e == "info")
    assert info == {
        "battery": 100, "gain": 2, "capacity": (1, 2), "firmware": "v1", "sn": "G1000897",
    }
    await recorder.start(tmp_path)
    path = await recorder.stop()
    assert len(list(packets(path))) == 2
    await recorder.disconnect()
    assert not recorder.authorized


@pytest.mark.parametrize("value, expected", [
    (b"G1000897", "G1000897"),
    ("SN-设备-".encode() + b"1" * 80, "SN-设备-" + "1" * 80),
    (b"G1000897\0tail", "G1000897\0tail"),
    (b"", None),
    (b"\xff", None),
])
async def test_serial_number_from_protocol(value, expected):
    class SerialClient(FakeClient):
        serial_number = value

    events = []
    recorder = Recorder(lambda *e: events.append(e), SerialClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    assert next(data for name, data in events if name == "info")["sn"] == expected
    assert recorder.sn == expected
    request = next(frame for frame in recorder.client.writes if (frame.kind, frame.cmd) == (0, 12))
    assert request.body == b""
    assert encode(0, request.kind, request.cmd, request.body) == bytes.fromhex(
        "5A 00 E4 2C 02 00 00 0C"
    )
    assert recorder.authorized
    recorder.client.serial_number = b"NEW-SN"
    await recorder.refresh()
    assert [data for name, data in events if name == "info"][-1]["sn"] == "NEW-SN"
    assert recorder.sn == "NEW-SN"
    await recorder.disconnect()
    assert recorder.sn is None


async def test_serial_failure_does_not_prevent_connection():
    class SerialClient(FakeClient):
        serial_number = b"SN"

        async def write_gatt_char(self, char, data, response):
            frame = Parser().feed(data)[0]
            if (frame.kind, frame.cmd) == (0, 12):
                raise OSError("read failed")
            await super().write_gatt_char(char, data, response)

    events = []
    recorder = Recorder(lambda *e: events.append(e), SerialClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    assert recorder.authorized
    assert next(data for name, data in events if name == "info")["sn"] is None
    await recorder.disconnect()


async def test_serial_timeout_continues_info_and_ignores_late_reply():
    class SilentSerialClient(FakeClient):
        async def write_gatt_char(self, char, data, response):
            frame = Parser().feed(data)[0]
            if (frame.kind, frame.cmd) != (0, 12):
                await super().write_gatt_char(char, data, response)

    class FastRecorder(Recorder):
        async def request(self, kind, cmd, reply_cmd, *args, **kwargs):
            if (kind, cmd) == (0, 12):
                kwargs["timeout"] = 0.01
            return await super().request(kind, cmd, reply_cmd, *args, **kwargs)

    events = []
    recorder = FastRecorder(lambda *e: events.append(e), SilentSerialClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    assert recorder.authorized and recorder.client.is_connected
    info = next(data for name, data in events if name == "info")
    assert info["sn"] is None and info["firmware"] == "v1" and info["battery"] == 100
    client = recorder.client
    client.emit(0, 13, b"LATE-SN")
    assert len([1 for name, _ in events if name == "info"]) == 1
    assert info["sn"] is None
    await recorder.disconnect()
    count = len(events)
    client.emit(0, 13, b"OLD-SN")
    assert len(events) == count


@pytest.mark.parametrize("reply", [b"", b"\x01"])
async def test_failed_auth_disconnects(reply):
    class DeniedClient(FakeClient):
        auth_reply = reply

    recorder = Recorder(client_factory=DeniedClient)
    with pytest.raises(ValueError, match="验证失败"):
        await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    assert not recorder.authorized
    assert recorder.client is None


async def test_ack_is_not_auth_success():
    class SilentClient(FakeClient):
        async def write_gatt_char(self, *args, **kwargs):
            pass

    recorder = Recorder(client_factory=SilentClient)
    recorder.client = SilentClient(None, recorder._disconnected)
    await recorder.client.connect()
    recorder.write_char = recorder.client.services.get_characteristic(None)
    with pytest.raises(TimeoutError, match="结果未确认"):
        await recorder.request(0, 50, 51, b"x" * 32, require_auth=False, timeout=0.01)
    assert recorder.client is None


async def test_pending_command_fails_on_disconnect():
    recorder = Recorder(client_factory=FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    task = asyncio.create_task(recorder.request(9, 9, 10))
    await asyncio.sleep(0)
    await recorder.disconnect()
    with pytest.raises(ConnectionError):
        await task


async def test_no_business_commands_before_auth():
    recorder = Recorder(client_factory=FakeClient)
    recorder.client = FakeClient(None, recorder._disconnected)
    await recorder.client.connect()
    with pytest.raises(PermissionError):
        await recorder.send(1, 0)


@pytest.mark.parametrize("channel", [MAIN, KEY])
async def test_key_event_one_is_forwarded_only_after_auth(channel):
    events = []
    recorder = Recorder(lambda *e: events.append(e), FakeClient)
    recorder._notify(Parser(), encode(1, 3, 1))
    assert not events
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    notify = recorder.client.notify[channel]
    frame = encode(2, 3, 1)
    notify(None, frame[:4])
    notify(None, frame[4:])
    assert [value for name, value in events if name == "key_event"] == [1]
    notify(None, encode(3, 3, 2, b"\x01") + encode(4, 0, 1))
    assert [value for name, value in events if name == "key_event"] == [1]
    await recorder.disconnect()


async def test_audio_events_cover_first_and_stop_tail_packets(tmp_path):
    events = []
    recorder = Recorder(lambda *e: events.append(e), FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    await recorder.start(tmp_path)
    await recorder.stop()
    names = [name for name, _ in events]
    assert names.index("capture_started") < names.index("audio")
    assert names.count("audio") == 2
    assert names[-1] == "capture_closed"
    await recorder.disconnect()


async def test_stop_key_is_forwarded_and_duplicate_frames_are_ignored():
    events = []
    recorder = Recorder(lambda *e: events.append(e), FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    start = encode(10, 3, 1)
    stop = encode(11, 3, 3)
    for channel in (MAIN, KEY):
        recorder.client.notify[channel](None, start)
    for channel in (MAIN, KEY):
        recorder.client.notify[channel](None, stop)
    assert [v for name, v in events if name == "key_event"] == [1, 3]
    # A fresh press with a new sequence must not be discarded as a repeated notification.
    recorder.client.notify[KEY](None, encode(12, 3, 1))
    assert [v for name, v in events if name == "key_event"] == [1, 3, 1]
    await recorder.disconnect()


async def test_stop_key_stops_receipt_and_retains_tail_audio(tmp_path):
    events, stops = [], []

    def event(name, value):
        events.append((name, value))
        if name == "key_event" and value == 3:
            stops.append(asyncio.create_task(recorder.stop()))

    recorder = Recorder(event, FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    await recorder.start(tmp_path)
    client = recorder.client
    client.emit(3, 3)
    path = await stops[0]
    assert (client.writes[-1].kind, client.writes[-1].cmd) == (1, 2)
    assert len(list(packets(path))) == 2
    assert recorder.capture is None
    client.emit(1, 1, b"late audio")
    assert len([name for name, _ in events if name == "audio"]) == 2
    assert len(list(packets(path))) == 2
    await recorder.disconnect()


async def test_device_stopped_before_key_handler_is_not_an_error(tmp_path):
    recorder = Recorder(client_factory=FakeClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    await recorder.start(tmp_path)
    client = recorder.client
    client.emit(3, 3)
    client.emit(1, 4, b"\x02")
    assert recorder.capture is None
    path = await recorder.stop()
    assert path.is_file()
    assert len(list(packets(path))) == 1
    assert (client.writes[-1].kind, client.writes[-1].cmd) == (1, 0)
    await recorder.disconnect()
