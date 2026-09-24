"""Minimal ABI encoding and decoding: 32-byte words, addresses, strings.

Every decoder returns None on a short or malformed input instead of guessing:
a number that could not be read is never shown as zero.
"""

from .keccak import keccak256

ZERO_ADDRESS = "0x" + "0" * 40


def to_bytes(hex_str: str) -> bytes:
    h = hex_str[2:] if hex_str.startswith(("0x", "0X")) else hex_str
    return bytes.fromhex(h)


def is_address(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.lower().startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def norm_address(value: str) -> str:
    return value.lower()


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()[:8]


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


def word(data: bytes, i: int) -> bytes | None:
    chunk = data[32 * i:32 * i + 32]
    return chunk if len(chunk) == 32 else None


def word_uint(data: bytes, i: int) -> int | None:
    w = word(data, i)
    return int.from_bytes(w, "big") if w is not None else None


def word_int(data: bytes, i: int) -> int | None:
    """A signed word. int128/int24 values are sign-extended to 256 bits by the ABI."""
    w = word(data, i)
    return int.from_bytes(w, "big", signed=True) if w is not None else None


def word_address(data: bytes, i: int) -> str | None:
    w = word(data, i)
    if w is None or any(w[:12]):
        return None
    return "0x" + w[12:].hex()


def topic_address(topic: str) -> str | None:
    b = to_bytes(topic)
    if len(b) != 32 or any(b[:12]):
        return None
    return "0x" + b[12:].hex()


def encode_address(addr: str) -> str:
    return addr.lower()[2:].rjust(64, "0")


def encode_uint(value: int) -> str:
    return format(value, "064x")


def call_data(sel: str, *addresses: str) -> str:
    return sel + "".join(encode_address(a) for a in addresses)


def address_topic(addr: str) -> str:
    return "0x" + encode_address(addr)


def decode_string(ret_hex: str) -> str | None:
    """An ABI `string` return, or a legacy `bytes32` one (some old tokens)."""
    try:
        data = to_bytes(ret_hex)
    except ValueError:
        return None
    if len(data) == 32:
        raw = data.rstrip(b"\0")
        if not raw or b"\0" in raw:
            return None  # a number, not a left-aligned bytes32 string
        text = raw.decode("utf-8", "replace")
        return text if text.isprintable() else None
    offset = word_uint(data, 0)
    if offset is None or offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset:offset + 32], "big")
    raw = data[offset + 32:offset + 32 + length]
    if len(raw) != length:
        return None
    text = raw.decode("utf-8", "replace")
    return "".join(c for c in text if c.isprintable()) or None


def decode_uint(ret_hex: str) -> int | None:
    try:
        data = to_bytes(ret_hex)
    except ValueError:
        return None
    if len(data) != 32:
        return None
    return int.from_bytes(data, "big")
