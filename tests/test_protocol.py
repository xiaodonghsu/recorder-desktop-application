import binascii
import struct

import pytest

from voicehub.protocol import MAIN, SERVICE, Parser, auth_key, encode


def test_crc_and_auth_frame():
    assert binascii.crc_hqx(b"123456789", 0) == 0x31C3
    assert SERVICE.startswith("0000ae20")
    assert MAIN.startswith("0000ae22")
    frame = encode(255, 0, 50, auth_key(" 0X" + "ab" * 32 + " "))
    assert len(frame) == 40
    assert struct.unpack_from("<H", frame, 4)[0] == 34
    assert Parser().feed(frame)[0].body == bytes.fromhex("ab" * 32)


@pytest.mark.parametrize("value", ["", "a" * 63, "gg" * 32, "aa " * 32])
def test_auth_rejects_invalid(value):
    with pytest.raises(ValueError):
        auth_key(value)


def test_fragment_concat_noise_crc_and_ack():
    parser = Parser()
    good = encode(3, 0, 4, b"\x55")
    bad = bytearray(good)
    bad[-1] ^= 1
    assert parser.feed(b"noise" + good[:4]) == []
    frames = parser.feed(good[4:] + bad + good)
    assert [f.body for f in frames] == [b"\x55", b"\x55"]
    assert parser.crc_errors == 1
    tail = b"\x01\x00\x03"
    ack = b"\x5a\x00" + struct.pack("<H", binascii.crc_hqx(tail, 0)) + tail
    assert parser.feed(ack)[0].cmd is None


def test_channels_keep_separate_fragments():
    main, key = Parser(), Parser()
    frame = encode(1, 0, 51, b"\0")
    assert main.feed(frame[:5]) == []
    assert key.feed(encode(1, 3, 1))
    assert main.feed(frame[5:])[0].cmd == 51
