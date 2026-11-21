from types import SimpleNamespace

from PySide6.QtCore import QSettings

from voicehub.credentials import DeviceCredentials


def test_device_credentials_survive_reload_without_plaintext_key(tmp_path):
    path = str(tmp_path / "devices.ini")
    passwords = {}
    secrets = SimpleNamespace(
        set_password=lambda service, name, key: passwords.update({(service, name): key}),
        get_password=lambda service, name: passwords.get((service, name)),
    )
    store = DeviceCredentials(QSettings(path, QSettings.Format.IniFormat), secrets)
    store.remember("CAC10 测试", "aa-bb-cc-dd-ee-ff", "0X" + "AB" * 32)
    reloaded = DeviceCredentials(QSettings(path, QSettings.Format.IniFormat), secrets)
    assert reloaded.key_for("AA:BB:CC:DD:EE:FF") == "ab" * 32
    assert reloaded.key_for("11:22:33:44:55:66") == ""
    assert reloaded.devices()["AA:BB:CC:DD:EE:FF"]["name"] == "CAC10 测试"
    assert "ab" * 32 not in (tmp_path / "devices.ini").read_text(encoding="utf-8")
    store.remember("CAC10 renamed", "AA:BB:CC:DD:EE:FF", "cd" * 32)
    assert len(reloaded.devices()) == 1
    assert reloaded.key_for("aa-bb-cc-dd-ee-ff") == "cd" * 32
