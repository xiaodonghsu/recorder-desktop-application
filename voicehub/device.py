import asyncio
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from .capture import Capture
from .library import download_names, parse_file_list
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
        self.file_handler = None
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
        if self.file_handler:
            self.file_handler(None)
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
            if frame.kind == 2 and self.file_handler:
                self.file_handler(frame)
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

    async def request(
        self, kind, cmd, reply_cmd, body=b"", require_auth=True, timeout=5,
        disconnect_on_timeout=True,
    ):
        async with self.lock:
            future = asyncio.get_running_loop().create_future()
            self.pending = ((kind, reply_cmd), future)
            try:
                await self.send(kind, cmd, body, require_auth)
                return await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                if not disconnect_on_timeout:
                    raise TimeoutError(f"命令 {kind}-{cmd} 超时，结果未确认") from exc
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
        info = {"sn": None}
        for name, kind, cmd, reply, size in (
            ("sn", 0, 12, 13, 1),
            ("battery", 0, 3, 4, 1),
            ("gain", 3, 25, 26, 1),
            ("capacity", 0, 1, 2, 8),
            ("firmware", 0, 10, 11, 1),
        ):
            try:
                if name == "sn":
                    # PNote.getSn(): optional read-only query; failure must not
                    # disconnect an authenticated device or skip other information.
                    value = await self.request(kind, cmd, reply, disconnect_on_timeout=False)
                else:
                    value = await self.request(kind, cmd, reply)
                if len(value) < size:
                    raise ValueError(f"{name} 应答长度不足")
                if name == "sn":
                    info[name] = value.decode("utf-8")
                elif name == "capacity":
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

    def _check_files_idle(self):
        if self.capture or self.stream_uncertain or self.file_handler:
            raise RuntimeError("请先停止录音或完成文件操作")
        if not self.authorized:
            raise PermissionError("请先验证 AuthKey")

    async def list_files(self):
        async with self.lock:
            self._check_files_idle()
            future = asyncio.get_running_loop().create_future()
            entries = []

            def receive(frame):
                if future.done():
                    return
                try:
                    if frame is None:
                        raise ConnectionError("读取文件列表时连接断开")
                    if frame.cmd == 1:
                        entries.extend(parse_file_list(frame.body))
                        if len(entries) > 100000:
                            raise ValueError("文件列表过大")
                    elif frame.cmd == 18:
                        if frame.body[:1] != b"\0":
                            raise ValueError("设备未确认文件列表完成")
                        future.set_result(entries)
                except (ValueError, OSError, UnicodeError) as exc:
                    future.set_exception(exc)

            self.file_handler = receive
            try:
                await self.send(2, 0)
                return await asyncio.wait_for(future, 15)
            except TimeoutError as exc:
                await self.disconnect()
                raise TimeoutError("读取文件列表超时，未收到完整列表结束确认；请重新连接") from exc
            except BaseException:
                await self.disconnect()
                raise
            finally:
                self.file_handler = None
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def delete_file(self, entry):
        self._check_files_idle()
        raw = bytes.fromhex(entry["raw"])
        if len(raw) not in (28, 33):
            raise ValueError("无效的文件列表条目")
        reply = await self.request(2, 8, 13, raw)
        if reply[:1] != b"\0":
            raise ValueError("录音卡未确认删除成功")

    async def download_file(self, entry, target, name_width=29):
        async with self.lock:
            self._check_files_idle()
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".part")
            if target.exists() or partial.exists():
                raise FileExistsError("目标录音已存在，请使用新的本地记录")
            try:
                for name in download_names(entry["name"]):
                    encoded = name.encode("utf-8")
                    if len(encoded) > name_width:
                        raise ValueError("下载文件名超出协议字段长度")
                    future = asyncio.get_running_loop().create_future()
                    received = 0
                    activity = time.monotonic()
                    with partial.open("wb") as output:

                        def receive(frame, future=future, output=output):
                            nonlocal received, activity
                            if future.done():
                                return
                            try:
                                if frame is None:
                                    raise ConnectionError("下载时蓝牙连接断开")
                                if frame.cmd not in (3, 4, 5):
                                    return
                                activity = time.monotonic()
                                if frame.cmd == 4:
                                    received += len(frame.body)
                                    if received > 512 * 1024 * 1024:
                                        raise ValueError("单文件下载超过 512 MiB 限制")
                                    output.write(frame.body)
                                elif frame.cmd == 5:
                                    future.set_result(frame.body[0] if frame.body else 3)
                            except (ValueError, OSError) as exc:
                                future.set_exception(exc)

                        self.file_handler = receive
                        await self.send(
                            2, 2, struct.pack("<I", 0) + encoded.ljust(name_width, b"\0")
                        )
                        while not future.done():
                            await asyncio.wait({future}, timeout=1)
                            self.event("file_progress", f"{entry['name']}：已下载 {received} 字节")
                            if time.monotonic() - activity > 12:
                                raise TimeoutError("文件下载超时，请重连后重试；部分数据已保留")
                        code = future.result()
                    self.file_handler = None
                    if code == 1 and not received:
                        continue
                    if code != 0 or not received:
                        raise ValueError(f"文件下载未完成（状态 {code}，{received} 字节）")
                    partial.replace(target)
                    return target
                raise FileNotFoundError("设备中找不到该文件（已尝试补全 .opus 和 .wav）")
            except BaseException:
                # Disconnect makes delayed file frames ineligible for a later operation.
                await self.disconnect()
                raise
            finally:
                self.file_handler = None
                if "future" in locals():
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()

    async def start(self, root):
        if self.capture or self.stream_uncertain or self.file_handler:
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
