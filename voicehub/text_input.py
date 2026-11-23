"""Type confirmed dictation into the selected Windows foreground control.

Uses Unicode keyboard input, so neither the clipboard nor the keyboard layout
needs changing. Explicit voice commands can send configured key combinations.
"""

import ctypes
import os
import sys
from dataclasses import dataclass

from .voice_commands import KEY_ACTIONS

DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
LONG = ctypes.c_int32
HWND = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left/right Win


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("flags", DWORD),
        ("hwndActive", HWND),
        ("hwndFocus", HWND),
        ("hwndCapture", HWND),
        ("hwndMenuOwner", HWND),
        ("hwndMoveSize", HWND),
        ("hwndCaret", HWND),
        ("rcCaret", RECT),
    ]


@dataclass(frozen=True)
class InputTarget:
    window: int
    focus: int
    process_id: int


def _load_user32():
    if sys.platform != "win32":
        raise RuntimeError("自动文字输入目前仅支持 Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = HWND
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.GetGUIThreadInfo.argtypes = [DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    return user32


class TextInjector:
    def __init__(self):
        self.user32 = _load_user32()

    def capture_target(self):
        """Return an external foreground control, or None if it is unavailable."""
        window = self.user32.GetForegroundWindow()
        if not window:
            return None
        pid = DWORD()
        thread = self.user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not thread or pid.value == os.getpid():
            return None
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return None
        # Avoid delivering text while the thread operates a menu or moves a window.
        if info.flags & 0x1E:
            return None
        if info.hwndActive != window or not info.hwndFocus:
            return None
        if self.user32.GetForegroundWindow() != window:
            return None
        return InputTarget(window, info.hwndFocus, pid.value)

    def send_key(self, action, target):
        """Send one configured shortcut, releasing only synthesized keys on failure."""
        if action not in KEY_ACTIONS:
            raise ValueError("不支持的语音指令按键")
        if target is None or self.capture_target() != target:
            raise RuntimeError("输入窗口或焦点已改变，自动输入已暂停")
        if any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in MODIFIER_KEYS):
            raise RuntimeError("检测到修饰键按下，自动输入已暂停；请松开 Shift/Ctrl/Alt/Win")
        keys = KEY_ACTIONS[action]
        events = [(key, 0) for key in keys] + [(key, KEYEVENTF_KEYUP) for key in reversed(keys)]
        inputs = (INPUT * len(events))()
        for index, (key, flags) in enumerate(events):
            inputs[index].type = INPUT_KEYBOARD
            inputs[index].ki = KEYBDINPUT(key, 0, flags, 0, 0)
        if self.capture_target() != target:
            raise RuntimeError("输入窗口或焦点已改变，自动输入已暂停")
        sent = self.user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            pressed = []
            for key, flags in events[:sent]:
                if flags == 0:
                    pressed.append(key)
                else:
                    pressed.remove(key)
            if pressed:
                releases = (INPUT * len(pressed))()
                for index, key in enumerate(reversed(pressed)):
                    releases[index].type = INPUT_KEYBOARD
                    releases[index].ki = KEYBDINPUT(key, 0, KEYEVENTF_KEYUP, 0, 0)
                self.user32.SendInput(len(releases), releases, ctypes.sizeof(INPUT))
            raise RuntimeError(f"按键未完整发送（{sent}/{len(inputs)}），请核对目标应用")

    def send(self, text, target):
        """Submit one Unicode segment, failing rather than retargeting or retrying."""
        if not isinstance(text, str):
            raise TypeError("输入内容必须是文字")
        if any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in text):
            raise ValueError("自动输入不发送换行、Tab 或其他控制字符")
        if not text:
            return
        if target is None or self.capture_target() != target:
            raise RuntimeError("输入窗口或焦点已改变，自动输入已暂停；文字保留在 VoiceHub")
        if any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in MODIFIER_KEYS):
            raise RuntimeError("检测到修饰键按下，自动输入已暂停；请松开 Shift/Ctrl/Alt/Win")
        encoded = text.encode("utf-16-le")
        units = [
            int.from_bytes(encoded[index : index + 2], "little")
            for index in range(0, len(encoded), 2)
        ]
        inputs = (INPUT * (len(units) * 2))()
        for index, unit in enumerate(units):
            for offset, flags in enumerate(
                (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            ):
                inputs[index * 2 + offset].type = INPUT_KEYBOARD
                inputs[index * 2 + offset].ki = KEYBDINPUT(0, unit, flags, 0, 0)
        # Revalidate after constructing the array; never activate the old window.
        if self.capture_target() != target:
            raise RuntimeError("输入窗口或焦点已改变，自动输入已暂停；文字保留在 VoiceHub")
        sent = self.user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            if sent % 2:
                # A partial submission can end on a keydown. Release only that
                # synthesized Unicode key; never release the user's modifiers.
                release = INPUT()
                release.type = INPUT_KEYBOARD
                release.ki = KEYBDINPUT(
                    0, inputs[sent - 1].ki.wScan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0
                )
                self.user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(INPUT))
            raise RuntimeError(
                f"文字输入未完整发送（{sent}/{len(inputs)} 个键盘事件），自动输入已暂停；"
                "目标应用可能有权限限制。请核对已输入内容，避免重复粘贴"
            )
