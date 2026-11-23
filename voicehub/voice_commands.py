"""Exact, whole-utterance voice command mappings."""

import unicodedata

DEFAULT_MAPPINGS = "回车 -> Enter\n换行 -> Shift+Enter"
KEY_ACTIONS = {
    "Enter": (0x0D,),
    "Shift+Enter": (0x10, 0x0D),
    "Ctrl+Enter": (0x11, 0x0D),
    "Tab": (0x09,),
}


def normalize_command(text):
    # Ignore surrounding ASR punctuation/space, never words or internal punctuation.
    text = unicodedata.normalize("NFKC", text).strip()
    while text and (text[0].isspace() or unicodedata.category(text[0]).startswith("P")):
        text = text[1:]
    while text and (text[-1].isspace() or unicodedata.category(text[-1]).startswith("P")):
        text = text[:-1]
    return text


def parse_mappings(value):
    mappings = {}
    for number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        phrase, separator, action = line.partition("->")
        phrase, action = normalize_command(phrase), action.strip()
        if not separator or not phrase or action not in KEY_ACTIONS:
            raise ValueError(f"第 {number} 行格式应为：指令 -> 按键；支持 {', '.join(KEY_ACTIONS)}")
        if phrase in mappings:
            raise ValueError(f"第 {number} 行指令重复：{phrase}")
        mappings[phrase] = action
    return mappings
