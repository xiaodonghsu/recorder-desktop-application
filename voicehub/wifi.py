"""Windows hotspot association and the recorder's TCP receive channel."""

import asyncio
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from xml.sax.saxutils import escape


def validate_password(password):
    if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
        raise ValueError("WiFi 密码须为 8–63 位可打印 ASCII 字符")


def profile_xml(ssid, password, name):
    validate_password(password)
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("无效的录音卡热点名称")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{escape(name)}</name>
  <SSIDConfig><SSID><hex>{ssid.encode('utf-8').hex()}</hex></SSID></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>WPA2PSK</authentication>
  <encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
  <sharedKey><keyType>passPhrase</keyType><protected>false</protected>
  <keyMaterial>{escape(password)}</keyMaterial></sharedKey></security></MSM>
</WLANProfile>'''


class WindowsWifi:
    def __init__(self):
        self.profile = None

    async def _netsh(self, *args):
        if os.name != "nt":
            raise OSError("自动连接录音卡热点目前仅支持 Windows")

        def run():
            try:
                result = subprocess.run(
                    ["netsh", "wlan", *args], check=False, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
                )
            except subprocess.TimeoutExpired as exc:
                raise OSError("Windows WiFi 操作超时，请检查 WLAN 服务") from exc
            if result.returncode:
                raise OSError("Windows WiFi 操作失败，请检查无线网卡、WLAN 服务和系统权限")
            return result.stdout.decode("oem", errors="replace")

        return await asyncio.to_thread(run)

    async def connected(self, ssid):
        output = await self._netsh("show", "interfaces")
        # SSID is an invariant field name in localized netsh output; exclude BSSID.
        return ssid in re.findall(r"^\s*SSID\s*:\s*(.*?)\s*$", output, re.MULTILINE)

    async def connect(self, ssid, password):
        if await self.connected(ssid):
            return
        await self.close()
        name = "VoiceHub-" + uuid.uuid4().hex
        xml = profile_xml(ssid, password, name)
        with tempfile.TemporaryDirectory(prefix="voicehub-wifi-") as directory:
            path = Path(directory) / "profile.xml"
            path.write_text(xml, encoding="utf-8")
            await self._netsh("add", "profile", f"filename={path}", "user=current")
        self.profile = name
        try:
            await self._netsh("connect", f"name={name}", f"ssid={ssid}")
            for _ in range(30):
                if await self.connected(ssid):
                    return
                await asyncio.sleep(1)
            raise TimeoutError("电脑连接录音卡热点超时，请检查热点密码和无线网卡")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.profile:
            name, self.profile = self.profile, None
            await self._netsh("delete", "profile", f"name={name}")


class TcpTransport:
    def __init__(self, receive, disconnected, host="192.168.1.1", port=27689):
        self.receive = receive
        self.disconnected = disconnected
        self.host, self.port = host, port
        self.writer = None
        self.tasks = []

    @property
    def is_connected(self):
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self):
        await self.close()
        reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=1 << 20), 10,
        )
        self.tasks = [asyncio.create_task(self._read(reader)), asyncio.create_task(self._heartbeat())]

    async def _read(self, reader):
        try:
            while data := await reader.read(1 << 20):
                self.receive(data)
            raise ConnectionError("录音卡 TCP 连接已关闭")
        except (OSError, ValueError) as exc:
            self.disconnected(str(exc))
        finally:
            await self.close()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(30)
                self.writer.write(b"\x01")
                await self.writer.drain()
        except OSError as exc:
            self.disconnected(str(exc))
            await self.close()

    async def close(self):
        current = asyncio.current_task()
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            if task is not current:
                task.cancel()
        if tasks:
            await asyncio.gather(*(t for t in tasks if t is not current), return_exceptions=True)
        writer, self.writer = self.writer, None
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass
