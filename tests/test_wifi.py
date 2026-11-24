import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from test_device import FakeClient

from voicehub.device import Recorder
from voicehub.protocol import Parser, encode, encode_wifi_password
from voicehub.wifi import TcpTransport, WindowsWifi, profile_xml


class WifiClient(FakeClient):
    hotspot_reply = b"\0Recorder-AP\0"
    password_reply = b"\0"

    async def write_gatt_char(self, char, data, response):
        await super().write_gatt_char(char, data, response)
        frame = Parser().feed(data)[0]
        if (frame.kind, frame.cmd) == (0, 24):
            self.emit(0, 23, b"\0")
        elif (frame.kind, frame.cmd) == (0, 19):
            self.emit(0, 20, self.hotspot_reply)
        elif (frame.kind, frame.cmd) == (0, 21):
            self.emit(0, 22)
        elif (frame.kind, frame.cmd) == (0, 62):
            assert int.from_bytes(frame.body[:2], "big") == len(frame.body[2:])
            self.emit(0, 63, self.password_reply)


class FakeWifi:
    def __init__(self):
        self.ssid = None

    async def connected(self, ssid):
        return self.ssid == ssid

    async def connect(self, ssid, password):
        assert password == "test-pass"
        self.ssid = ssid

    async def close(self):
        self.ssid = None


async def recorder_with_server(handler, client=WifiClient):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    recorder = Recorder(client_factory=client)
    recorder.tcp.host = "127.0.0.1"
    recorder.tcp.port = server.sockets[0].getsockname()[1]
    recorder.wifi = FakeWifi()
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    return recorder, server


@pytest.mark.parametrize("filename,width", [("note.", 24), ("note_2026-11-02-07-46-02.", 29)])
async def test_hotspot_tcp_file_split_coalesced_and_ble_duplicates(tmp_path, filename, width):
    requested = asyncio.Event()
    finished = asyncio.Event()

    async def serve(reader, writer):
        try:
            await requested.wait()
            stream = encode(1, 2, 3, b"note.wav") + encode(2, 2, 4, b"a" * 1024)
            writer.write(stream[:4])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(stream[4:] + encode(3, 2, 4, b"b" * 1024) + encode(4, 2, 5, b"\0"))
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    class Client(WifiClient):
        async def write_gatt_char(self, char, data, response):
            await super().write_gatt_char(char, data, response)
            frame = Parser().feed(data)[0]
            if (frame.kind, frame.cmd) == (2, 2):
                assert len(data) == 12 + width
                assert frame.body == bytes(4) + (
                    filename.rstrip(".") + ".opus"
                ).encode().ljust(width, b"\0")
                self.emit(2, 4, b"must-not-be-written")
                self.emit(2, 5, b"\0")
                requested.set()

    recorder, server = await recorder_with_server(serve, Client)
    try:
        await recorder.prepare_high_speed("test-pass")
        controls = [(f.kind, f.cmd) for f in recorder.client.writes if f.cmd in (19, 21, 24, 62)]
        assert controls == [(0, 62), (0, 21), (0, 19)]
        # Reusing an established TCP connection must not wait for a missing 0-23 reply.
        count = len(recorder.client.writes)
        await recorder.prepare_high_speed("test-pass")
        assert len(recorder.client.writes) == count
        target = tmp_path / "audio.opus"
        await recorder.download_file({"name": filename}, target, high_speed=True)
        assert target.read_bytes() == b"a" * 1024 + b"b" * 1024
        await recorder.use_bluetooth_transfer()
        assert not recorder.tcp.is_connected
        assert recorder.client.writes[-1].cmd == 21
    finally:
        await recorder.disconnect()
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(finished.wait(), 2)


async def test_change_only_peer_notifications_and_hotspot_close():
    events = []
    recorder = Recorder(lambda *event: events.append(event), WifiClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    try:
        recorder.client.emit(0, 20, b"\0PONE\0")
        assert recorder.wifi_hotspot_enabled is True
        assert recorder.wifi_ssid == "PONE"
        recorder.client.emit(0, 23, b"\x01")
        assert recorder.wifi_peer_connected is True
        recorder.client.emit(0, 23, b"\0")
        assert recorder.wifi_peer_connected is False
        failures = []
        recorder.file_handler = failures.append
        recorder.file_source = "tcp"

        class Writer:
            closing = False

            def is_closing(self):
                return self.closing

            def close(self):
                self.closing = True

            async def wait_closed(self):
                pass

        writer = Writer()
        recorder.tcp.writer = writer
        recorder.client.emit(0, 22)
        assert not recorder.tcp.is_connected
        assert writer.closing
        assert failures == [None]
        await recorder.wifi_cleanup_task
        assert recorder.wifi_hotspot_enabled is False
        assert recorder.wifi_ssid is None
        assert ("wifi_status", "录音卡 WiFi 热点已关闭") in events
    finally:
        recorder.file_handler = None
        recorder.file_source = None
        await recorder.disconnect()


async def test_open_failure_and_empty_reply_do_not_connect():
    class Failed(WifiClient):
        hotspot_reply = b"\x01"

    recorder = Recorder(client_factory=Failed)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    try:
        with pytest.raises(ValueError, match="未确认开启"):
            await recorder.prepare_high_speed("test-pass")
        assert recorder.wifi_hotspot_enabled is False
        assert not recorder.tcp.is_connected
        recorder.client.emit(0, 20)
        assert recorder.wifi_hotspot_enabled is False
    finally:
        await recorder.disconnect()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_tcp_loss_or_crc_never_finalizes_partial(tmp_path, corrupt):
    requested = asyncio.Event()
    finished = asyncio.Event()

    async def serve(reader, writer):
        try:
            await requested.wait()
            writer.write(encode(1, 2, 4, b"retained"))
            await writer.drain()
            if corrupt:
                broken = bytearray(encode(2, 2, 4, b"bad"))
                broken[2] ^= 1
                writer.write(broken + encode(3, 2, 5, b"\0"))
                await writer.drain()
                await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    class Client(WifiClient):
        async def write_gatt_char(self, char, data, response):
            await super().write_gatt_char(char, data, response)
            if Parser().feed(data)[0].kind == 2:
                requested.set()

    recorder, server = await recorder_with_server(serve, Client)
    try:
        await recorder.prepare_high_speed("test-pass")
        target = tmp_path / "audio.opus"
        with pytest.raises(ConnectionError):
            await recorder.download_file({"name": "note."}, target, high_speed=True)
        assert not target.exists()
        assert target.with_suffix(".opus.part").read_bytes() == b"retained"
        assert not recorder.authorized
    finally:
        await recorder.disconnect()
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(finished.wait(), 2)


def test_wifi_profile_xml_escapes_credentials():
    xml = profile_xml('Recorder<&', 'a<&"testpass', "VoiceHub-test")
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}
    assert root.find(".//w:keyMaterial", ns).text == 'a<&"testpass'
    assert bytes.fromhex(root.find(".//w:hex", ns).text).decode() == "Recorder<&"
    assert "<connectionMode>manual</connectionMode>" in xml


@pytest.mark.parametrize("password", ["short", "a" * 17, "中" * 17, None])
def test_invalid_wifi_password(password):
    with pytest.raises(ValueError):
        profile_xml("Recorder", password, "VoiceHub")


def test_password_frame_matches_published_capture_and_unicode_byte_length():
    assert encode(0, 0, 62, encode_wifi_password("12345678")).hex() == (
        "5a0019030c00003e00083132333435363738"
    )
    for password in ("中文热点密码测试", "😀" * 16):
        body = encode_wifi_password(password)
        assert body[:2] == len(password.encode("utf-8")).to_bytes(2, "big")
        assert body[2:].decode("utf-8") == password
        ElementTree.fromstring(profile_xml("Recorder", password, "VoiceHub"))


@pytest.mark.parametrize("reply", [b"\x01", b"", b"\0\0"])
async def test_password_rejection_never_opens_hotspot(reply):
    class Failed(WifiClient):
        password_reply = reply

    recorder = Recorder(client_factory=Failed)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    try:
        with pytest.raises(ValueError, match="密码设置成功"):
            await recorder.prepare_high_speed("test-pass")
        assert not recorder.tcp.is_connected
        assert recorder.wifi_password_digest is None
        assert recorder.client.writes[-1].cmd == 62
    finally:
        await recorder.disconnect()


async def test_password_unsupported_timeout_disconnects(monkeypatch):
    class Unsupported(WifiClient):
        async def write_gatt_char(self, char, data, response):
            if Parser().feed(data)[0].cmd == 62:
                return  # Firmware does not implement the SDK extension.
            await super().write_gatt_char(char, data, response)

    recorder = Recorder(client_factory=Unsupported)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    request = recorder.request

    async def fast_request(*args, **kwargs):
        kwargs["timeout"] = 0.001
        return await request(*args, **kwargs)

    monkeypatch.setattr(recorder, "request", fast_request)
    with pytest.raises(TimeoutError, match="固件可能不支持"):
        await recorder.prepare_high_speed("test-pass")
    assert not recorder.authorized
    assert not recorder.tcp.is_connected


async def test_invalid_password_does_not_send_ble():
    recorder = Recorder(client_factory=WifiClient)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    try:
        count = len(recorder.client.writes)
        with pytest.raises(ValueError, match="8–16"):
            await recorder.prepare_high_speed("too-short-password")
        assert len(recorder.client.writes) == count
    finally:
        await recorder.disconnect()


async def test_changed_password_restarts_active_hotspot_and_reconnects():
    events = []
    recorder = Recorder(lambda *event: events.append(event), WifiClient)
    recorder.wifi = FakeWifi()

    class Tcp:
        is_connected = False

        async def close(self):
            self.is_connected = False

        async def connect(self):
            self.is_connected = True

    recorder.tcp = Tcp()
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    passwords = []

    async def connect_wifi(ssid, password):
        passwords.append(password)
        recorder.wifi.ssid = ssid
        assert [f.cmd for f in recorder.client.writes[-3:]] == [62, 21, 19]

    recorder.wifi.connect = connect_wifi
    try:
        # Initial unknown state is explicitly closed after setting the password.
        await recorder.prepare_high_speed("test-pass")
        assert recorder.wifi_hotspot_enabled is True
        # Changing a password on an established session must restart the hotspot.
        await recorder.prepare_high_speed("new-pass")
        assert passwords == ["test-pass", "new-pass"]
        assert recorder.tcp.is_connected
        frame = next(f for f in reversed(recorder.client.writes) if f.cmd == 62)
        assert frame.body == b"\0\x08new-pass"
        assert "test-pass" not in str(events) and "new-pass" not in str(events)
    finally:
        await recorder.disconnect()
        assert recorder.wifi_password_digest is None


async def test_windows_wifi_profile_and_association(monkeypatch):
    wifi = WindowsWifi()
    calls = []
    polls = 0

    async def netsh(*args):
        nonlocal polls
        calls.append(args)
        if args == ("show", "interfaces"):
            polls += 1
            return "    SSID : Recorder\n    BSSID : 00:11:22:33:44:55\n" if polls > 1 else ""
        if args[:2] == ("add", "profile"):
            from pathlib import Path
            xml = Path(args[2].split("=", 1)[1]).read_text(encoding="utf-8")
            assert "test-pass" in xml
        return ""

    monkeypatch.setattr(wifi, "_netsh", netsh)
    await wifi.connect("Recorder", "test-pass")
    assert await wifi.connected("Recorder")
    assert not await wifi.connected("other")
    await wifi.close()
    assert calls[-1][:2] == ("delete", "profile")
    assert wifi.profile is None


async def test_tcp_heartbeat_failure_closes_transport(monkeypatch):
    disconnected = []
    transport = TcpTransport(lambda _: None, disconnected.append)

    class Writer:
        def write(self, data):
            assert data == b"\x01"
            raise OSError("socket failure")

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def sleep(_):
        pass

    monkeypatch.setattr("voicehub.wifi.asyncio.sleep", sleep)
    transport.writer = Writer()
    await transport._heartbeat()
    assert disconnected == ["socket failure"]
    assert not transport.is_connected
