import ctypes
import os

import pytest

from voicehub import text_input


class FakeUser32:
    def __init__(self):
        self.window = 10
        self.focus = 11
        self.pid = os.getpid() + 1
        self.thread = 20
        self.gui_ok = True
        self.flags = 0
        self.modifier = None
        self.calls = []
        self.sent = None

    def GetForegroundWindow(self):
        return self.window

    def GetWindowThreadProcessId(self, window, pid):
        assert window == self.window
        ctypes.cast(pid, ctypes.POINTER(text_input.DWORD)).contents.value = self.pid
        return self.thread

    def GetGUIThreadInfo(self, thread, info):
        assert thread == self.thread
        info = ctypes.cast(info, ctypes.POINTER(text_input.GUITHREADINFO)).contents
        assert info.cbSize == ctypes.sizeof(text_input.GUITHREADINFO)
        info.hwndActive = self.window
        info.hwndFocus = self.focus
        info.flags = self.flags
        return self.gui_ok

    def GetAsyncKeyState(self, key):
        return 0x8000 if key == self.modifier else 0

    def SendInput(self, count, inputs, size):
        assert size == ctypes.sizeof(text_input.INPUT)
        array = ctypes.cast(inputs, ctypes.POINTER(text_input.INPUT))
        self.calls.append(
            [
                (array[i].type, array[i].ki.wVk, array[i].ki.wScan, array[i].ki.dwFlags)
                for i in range(count)
            ]
        )
        return count if self.sent is None else min(self.sent, count)


@pytest.fixture
def injector(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(text_input, "_load_user32", lambda: fake)
    return text_input.TextInjector(), fake


def test_send_unicode_chinese_and_surrogate_pairs(injector):
    injector, fake = injector
    target = injector.capture_target()
    injector.send("中文 A🙂！", target)
    encoded = "中文 A🙂！".encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    expected = []
    for unit in units:
        expected.extend([(1, 0, unit, 4), (1, 0, unit, 6)])
    assert fake.calls == [expected]


@pytest.mark.parametrize("field,value", [("window", 99), ("focus", 99), ("pid", 99)])
def test_changed_target_never_receives_text(injector, field, value):
    injector, fake = injector
    target = injector.capture_target()
    setattr(fake, field, value)
    with pytest.raises(RuntimeError, match="焦点已改变"):
        injector.send("不应输入", target)
    assert not fake.calls


@pytest.mark.parametrize("state", ["own", "missing", "gui_error", "no_focus"])
def test_target_capture_refuses_unavailable_or_own_window(injector, state):
    injector, fake = injector
    if state == "own":
        fake.pid = os.getpid()
    elif state == "missing":
        fake.window = 0
    elif state == "gui_error":
        fake.gui_ok = False
    else:
        fake.focus = 0
    assert injector.capture_target() is None


@pytest.mark.parametrize("key", text_input.MODIFIER_KEYS)
def test_pressed_modifier_prevents_text(injector, key):
    injector, fake = injector
    target = injector.capture_target()
    fake.modifier = key
    with pytest.raises(RuntimeError, match="修饰键"):
        injector.send("text", target)
    assert not fake.calls


@pytest.mark.parametrize("flags", [0x02, 0x04, 0x08, 0x10])
def test_menu_or_window_move_state_is_not_a_text_target(injector, flags):
    injector, fake = injector
    fake.flags = flags
    assert injector.capture_target() is None


@pytest.mark.parametrize("text", ["line\nnext", "\r", "\t", "\x00", "\x7f", "\x85"])
def test_control_characters_never_create_submit_or_shortcut(injector, text):
    injector, fake = injector
    with pytest.raises(ValueError, match="控制字符"):
        injector.send(text, injector.capture_target())
    assert not fake.calls


@pytest.mark.parametrize("sent", [0, 1, 2])
def test_partial_send_is_not_retried_and_releases_only_unpaired_key(injector, sent):
    injector, fake = injector
    target = injector.capture_target()
    fake.sent = sent
    with pytest.raises(RuntimeError, match="未完整发送"):
        injector.send("中文", target)
    assert len(fake.calls) == (2 if sent == 1 else 1)
    if sent == 1:
        assert fake.calls[1] == [(1, 0, ord("中"), 6)]


def test_input_structure_matches_windows_abi():
    assert ctypes.sizeof(text_input.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


@pytest.mark.parametrize("action,keys", list(text_input.KEY_ACTIONS.items()))
def test_key_commands_press_and_release_in_order(injector, action, keys):
    injector, fake = injector
    injector.send_key(action, injector.capture_target())
    assert fake.calls == [
        [(1, key, 0, 0) for key in keys] + [(1, key, 0, 2) for key in reversed(keys)]
    ]


@pytest.mark.parametrize("sent", [0, 1, 2, 3])
def test_partial_shortcut_releases_synthesized_keys_without_retry(injector, sent):
    injector, fake = injector
    fake.sent = sent
    with pytest.raises(RuntimeError, match="未完整发送"):
        injector.send_key("Shift+Enter", injector.capture_target())
    expected_releases = {0: [], 1: [0x10], 2: [0x0D, 0x10], 3: [0x10]}
    if expected_releases[sent]:
        assert fake.calls[1] == [(1, key, 0, 2) for key in expected_releases[sent]]
    else:
        assert len(fake.calls) == 1


def test_commands_reject_changed_focus_modifiers_and_unknown_keys(injector):
    injector, fake = injector
    target = injector.capture_target()
    fake.focus = 99
    with pytest.raises(RuntimeError, match="焦点"):
        injector.send_key("Enter", target)
    fake.focus = target.focus
    fake.modifier = 0x10
    with pytest.raises(RuntimeError, match="修饰键"):
        injector.send_key("Shift+Enter", target)
    with pytest.raises(ValueError, match="不支持"):
        injector.send_key("Alt+F4", target)
    assert fake.calls == []


def test_non_windows_constructor_has_clear_error(monkeypatch):
    monkeypatch.setattr(text_input.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        text_input.TextInjector()
