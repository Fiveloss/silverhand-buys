"""Keccak-256 as Ethereum uses it (original Keccak padding, not NIST SHA3-256).

Pure Python so the bot needs no crypto package. It hashes event signatures,
selectors and Uniswap v4 PoolIds: a handful of calls per tracked token, never
a hot path.
"""

_MASK = (1 << 64) - 1
_RATE = 136  # bytes, for a 256-bit output

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets, indexed [x][y].
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rol(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _permute(a: list[list[int]]) -> list[list[int]]:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)]
             for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % _RATE:
        msg.append(0)
    msg[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), _RATE):
        for i in range(_RATE // 8):
            lane = int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], "little")
            state[i % 5][i // 5] ^= lane
        state = _permute(state)
    return b"".join(state[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
