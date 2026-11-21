"""Remember verified devices; keep AuthKeys in the operating system credential store."""

import json

import keyring
from PySide6.QtCore import QSettings

from .protocol import auth_key


class DeviceCredentials:
    service = "VoiceHub.CAC10"

    def __init__(self, settings=None, secrets=None):
        self.settings = settings if settings is not None else QSettings("VoiceHub", "Devices")
        self.secrets = secrets if secrets is not None else keyring

    @staticmethod
    def identity(address):
        return address.strip().upper().replace("-", ":")

    def devices(self):
        try:
            devices = json.loads(self.settings.value("verified_devices", "{}"))
        except (ValueError, TypeError):
            return {}
        return devices if isinstance(devices, dict) else {}

    def key_for(self, address):
        value = self.secrets.get_password(self.service, self.identity(address))
        return auth_key(value).hex() if value else ""

    def remember(self, name, address, key):
        identity = self.identity(address)
        value = auth_key(key).hex()
        self.secrets.set_password(self.service, identity, value)
        devices = self.devices()
        devices[identity] = {"name": name, "address": address}
        self.settings.setValue("verified_devices", json.dumps(devices, ensure_ascii=False))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise OSError("无法保存已验证设备信息")
