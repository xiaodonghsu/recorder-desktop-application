import pytest

from voicehub.voice_commands import DEFAULT_MAPPINGS, normalize_command, parse_mappings


@pytest.mark.parametrize("text", ["回车", " 回车。", "\t回车！ \n", "“回车”。"])
def test_whole_command_ignores_surrounding_space_and_punctuation(text):
    assert parse_mappings(DEFAULT_MAPPINGS).get(normalize_command(text)) == "Enter"


@pytest.mark.parametrize("text", ["请按回车", "回车继续", "回，车", "回 车", "回车。换行", "。"])
def test_embedded_commands_are_literal_text(text):
    assert normalize_command(text) not in parse_mappings(DEFAULT_MAPPINGS)


@pytest.mark.parametrize(
    "value", ["回车 Enter", " -> Enter", "回车 -> Alt+F4", "回车 -> Enter\n回车。 -> Tab"]
)
def test_invalid_or_ambiguous_settings_are_rejected(value):
    with pytest.raises(ValueError):
        parse_mappings(value)
