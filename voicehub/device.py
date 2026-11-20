import asyncio
import struct
import time

from bleak import BleakClient, BleakScanner

from .capture import Capture
from .protocol import KEY, MAIN, SERVICE, WRITE, Parser, auth_key, encode


async def scan(timeout=6):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [
        (device, adv)
        for device, adv in found.values()
        if (adv.local_name or device.name or "").upper().startswith("CAC10")
    ]


class Recorder:
    def __init__(self, event=lambda *_: None, client_factory=BleakClient):
        self.event = event
        self.client_factory = client_factory
        self.client = None
        self.authorized = False
        self.seq = 0
        self.pending = None
        self.lock = asyncio.Lock()
        self.capture = None
        self.live_ready = None
        self.live_stopped = None
        self.stream_uncertain = False
        self.key_notifications = {}
        self.device_stopped_capture = None

    def _close_capture(self, reason):
        if not self.capture:
            return None
        path = self.capture.close(reason)
        self.capture = None
        self.device_stopped_capture = path if reason == "device-stopped" else None
        self.event("capture_closed", str(path))
        return path

    def _disconnected(self, client):
        if client is not self.client:
            return
        self.authorized = False
        if self.pending and not self.pending[1].done():
            self.pending[1].set_exception(ConnectionError("蓝牙连接已断开"))
        if self.live_ready and not self.live_ready.done():
            self.live_ready.set_exception(ConnectionError("推流时蓝牙断开"))
        if self.live_stopped and not self.live_stopped.done():
            self.live_stopped.set_exception(ConnectionError("停止推流时蓝牙断开"))
        if self.capture:
            path = self._close_capture("disconnected")
            self.event("interrupted", str(path))
        self.event("disconnected", None)

    def _notify(self, parser, data):
        before = parser.crc_errors
        for frame in parser.feed(data):
            if frame.cmd is None:
                continue
            if self.pending and self.pending[0] == (frame.kind, frame.cmd):
                future = self.pending[1]
                if not future.done():
                    future.set_result(frame.body)
            if frame.kind == 3 and frame.cmd in (1, 3) and self.authorized:
                # The same notification can arrive on both AE22 and AE23.
                now = time.monotonic()
                self.key_notifications = {
                    key: stamp for key, stamp in self.key_notifications.items() if now - stamp < 0.5
                }
                key = (frame.seq, frame.cmd, frame.body)
                if key not in self.key_notifications:
                    self.key_notifications[key] = now
                    self.event("key_event", frame.cmd)
            if frame.kind != 1:
                continue
            if frame.cmd in (0, 1) and self.live_ready and not self.live_ready.done():
                self.live_ready.set_result(True)
            if frame.cmd == 4 and frame.body[:1] == b"\x02":
                if self.live_stopped and not self.live_stopped.done():
                    self.live_stopped.set_result(True)
                elif self.capture:
                    path = self._close_capture("device-stopped")
                    self.event("interrupted", str(path))
            if frame.cmd == 1 and self.capture:
                try:
                    self.capture.push(frame.body)
                    self.event("audio", frame.body)
                except (OSError, ValueError) as exc:
                    path = self._close_capture("capture-error")
                    self.stream_uncertain = True
                    self.event("interrupted", str(path))
                    self.event("warning", str(exc) + "；请断开重连后再录音")
        if parser.crc_errors > before:
            self.event("warning", "收到 CRC 损坏帧，已丢弃；音频可能缺失")

    async def connect(self, device, key):
        raw_key = auth_key(key)
        await self.disconnect()
        self.key_notifications.clear()
        self.stream_uncertain = False
        self.client = self.client_factory(device, disconnected_callback=self._disconnected)
        try:
            await self.client.connect()
            if self.client.services.get_service(SERVICE) is None:
                raise ValueError("设备不提供 AE20 录音服务")
            self.write_char = self.client.services.get_characteristic(WRITE)
            if self.write_char is None:
                raise ValueError("设备缺少 AE21 写入通道")
            for uuid in (MAIN, KEY):
                parser = Parser()
                try:
                    await self.client.start_notify(
                        uuid, lambda _, data, p=parser: self._notify(p, data)
                    )
                except Exception:
                    if uuid == MAIN:
                        raise
                    self.event("warning", "AE23 不可用，机身按键通知不可用")
            reply = await self.request(0, 50, 51, raw_key, require_auth=False)
            if not reply or reply[0] != 0:
                raise ValueError("AuthKey 验证失败")
            self.authorized = True
            self.event("authorized", device.name or "CAC10")
            await self.refresh()
        except BaseException:
            await self.disconnect()
            raise

    async def send(self, kind, cmd, body=b"", require_auth=True):
        if not self.client or not self.client.is_connected:
            raise ConnectionError("请先连接录音卡")
        if require_auth and not self.authorized:
            raise PermissionError("请先验证 AuthKey")
        packet = encode(self.seq, kind, cmd, body)
        self.seq = (self.seq + 1) & 255
        response = "write-without-response" not in self.write_char.properties
        if not response and len(packet) > self.write_char.max_write_without_response_size:
            if "write" in self.write_char.properties:
                response = True
            else:
                raise ValueError("蓝牙 MTU 无法完整发送协议帧，请重新连接设备")
        await self.client.write_gatt_char(self.write_char, packet, response=response)

    async def request(self, kind, cmd, reply_cmd, body=b"", require_auth=True, timeout=5):
        async with self.lock:
            future = asyncio.get_running_loop().create_future()
            self.pending = ((kind, reply_cmd), future)
            try:
                await self.send(kind, cmd, body, require_auth)
                return await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                # Reusing a TYPE/CMD after timeout can consume a delayed old response.
                await self.disconnect()
                raise TimeoutError(f"命令 {kind}-{cmd} 超时，结果未确认；已断开，请重连") from exc
            finally:
                self.pending = None
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # consume disconnect errors if write itself failed

    async def refresh(self):
        info = {}
        for name, kind, cmd, reply, size in (
            ("battery", 0, 3, 4, 1),
            ("gain", 3, 25, 26, 1),
            ("capacity", 0, 1, 2, 8),
            ("firmware", 0, 10, 11, 1),
        ):
            try:
                value = await self.request(kind, cmd, reply)
                if len(value) < size:
                    raise ValueError(f"{name} 应答长度不足")
                if name == "capacity":
                    info[name] = struct.unpack_from("<II", value)
                elif name == "firmware":
                    info[name] = value.split(b"\0")[0].decode("utf-8", errors="replace")
                else:
                    info[name] = value[0]
            except Exception as exc:
                self.event("warning", str(exc))
                if not self.authorized:
                    raise
        self.event("info", info)

    async def start(self, root):
        if self.capture or self.stream_uncertain:
            raise RuntimeError("已有录音或推流状态未确认，请先停止或重连")
        if not self.authorized:
            raise PermissionError("请先验证 AuthKey")
        self.device_stopped_capture = None
        self.capture = Capture(root)
        self.event("capture_started", str(self.capture.path))
        self.live_ready = asyncio.get_running_loop().create_future()
        try:
            await self.send(1, 0)
            await asyncio.wait_for(self.live_ready, 8)
            if self.capture is None:
                raise RuntimeError("设备已停止推流")
            self.event("recording", str(self.capture.path))
        except BaseException:
            await self.disconnect()
            raise
        finally:
            self.live_ready = None

    async def stop(self):
        if not self.capture:
            # The hardware may send state=stopped before the GUI handles its key event.
            if self.device_stopped_capture:
                path = self.device_stopped_capture
                self.device_stopped_capture = None
                return path
            raise RuntimeError("当前没有录音")
        self.live_stopped = asyncio.get_running_loop().create_future()
        reason = "stopped"
        try:
            await self.send(1, 2)
            await asyncio.wait_for(self.live_stopped, 5)
        except Exception as exc:  # noqa: BLE001 - retain captured audio on any transport failure
            reason = "stop-unconfirmed"
            self.stream_uncertain = True
            self.event("warning", f"停止未确认：{exc}；保留已接收音频，请重连")
        finally:
            self.live_stopped = None
        if self.capture:
            return self._close_capture(reason)
        raise ConnectionError("录音已中断，原始音频已保存")

    async def disconnect(self):
        self.authorized = False
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            finally:
                self._disconnected(self.client)
                self.client = None
