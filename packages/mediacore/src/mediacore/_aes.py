"""AES-128-CBC, just enough of it for the router client's login and request bodies.

The TP-Link web UI encrypts every ``/cgi_gdpr`` body with AES-128-CBC under a per-session key, so
:mod:`mediacore.router` needs a block cipher. That is the *only* thing in this workspace that
does, and pulling in ``cryptography`` or ``pycryptodome`` for it would add a compiled dependency
(and a wheel per platform) to an image that currently needs neither: ``mediacore`` declares
``pydantic``/``pyyaml``/``requests`` and nothing else. The payloads here are a few hundred bytes
once a minute, so a pure-Python implementation costs nothing measurable.

Correctness is pinned by the FIPS-197 known-answer vectors in ``tests/test_aes.py`` — this is
exactly the kind of code that must never be "probably right".

Not a general-purpose crypto module: 128-bit keys only, CBC only, PKCS#7 only, and no attempt at
constant-time behaviour. It authenticates nothing and must not be used where that matters.
"""

from __future__ import annotations

SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

BLOCK = 16
_KEY_LEN = 16


def _build_inv_sbox() -> bytes:
    out = bytearray(256)
    for i, v in enumerate(SBOX):
        out[v] = i
    return bytes(out)


INV_SBOX = _build_inv_sbox()


def _xtime(a: int) -> int:
    """Multiply by x in GF(2^8)."""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """Multiply two GF(2^8) elements (used only by the inverse MixColumns)."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 key schedule: 11 round keys of 16 bytes."""
    words = [list(key[i * 4 : i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    return [[b for word in words[4 * r : 4 * r + 4] for b in word] for r in range(11)]


#: ShiftRows / InvShiftRows as a fixed index permutation — clearer and faster than juggling rows.
_SHIFT = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
_UNSHIFT = (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)


def _encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    state = [block[i] ^ round_keys[0][i] for i in range(BLOCK)]
    for rnd in range(1, 11):
        state = [SBOX[b] for b in state]
        state = [state[i] for i in _SHIFT]
        if rnd != 10:  # the final round omits MixColumns
            mixed: list[int] = []
            for c in range(4):
                a0, a1, a2, a3 = state[4 * c : 4 * c + 4]
                mixed += [
                    _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3,
                    a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3,
                    a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3,
                    _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3),
                ]
            state = mixed
        state = [state[i] ^ round_keys[rnd][i] for i in range(BLOCK)]
    return bytes(state)


def _decrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    state = [block[i] ^ round_keys[10][i] for i in range(BLOCK)]
    for rnd in range(9, -1, -1):
        state = [state[i] for i in _UNSHIFT]
        state = [INV_SBOX[b] for b in state]
        state = [state[i] ^ round_keys[rnd][i] for i in range(BLOCK)]
        if rnd != 0:
            mixed: list[int] = []
            for c in range(4):
                a0, a1, a2, a3 = state[4 * c : 4 * c + 4]
                mixed += [
                    _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9),
                    _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13),
                    _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11),
                    _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14),
                ]
            state = mixed
    return bytes(state)


def pad(data: bytes) -> bytes:
    """PKCS#7 pad to the block size. A full-length input still gains a whole block of padding."""
    n = BLOCK - len(data) % BLOCK
    return data + bytes([n]) * n


def unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding, tolerating a malformed tail rather than raising.

    The caller is a best-effort client parsing whatever a router sent back; a garbled body must
    surface as unparseable text upstream, not as an exception out of the cipher.
    """
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= BLOCK and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC encrypt with PKCS#7 padding."""
    _check(key, iv)
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    data = pad(plaintext)
    for i in range(0, len(data), BLOCK):
        block = bytes(a ^ b for a, b in zip(data[i : i + BLOCK], prev, strict=True))
        prev = _encrypt_block(round_keys, block)
        out += prev
    return bytes(out)


def decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC decrypt, PKCS#7 stripped. A non-block-multiple input yields b"" rather than
    raising — see :func:`unpad` for why this layer stays quiet."""
    _check(key, iv)
    if not ciphertext or len(ciphertext) % BLOCK:
        return b""
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), BLOCK):
        block = ciphertext[i : i + BLOCK]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(round_keys, block), prev, strict=True))
        prev = block
    return bytes(unpad(bytes(out)))


def _check(key: bytes, iv: bytes) -> None:
    if len(key) != _KEY_LEN:
        raise ValueError(f"AES-128 needs a {_KEY_LEN}-byte key, got {len(key)}")
    if len(iv) != BLOCK:
        raise ValueError(f"CBC needs a {BLOCK}-byte IV, got {len(iv)}")
