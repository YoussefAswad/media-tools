from __future__ import annotations

import pytest
from mediacore import _aes

#: FIPS-197 Appendix C.1 known-answer vector for AES-128. The whole point of vendoring a cipher
#: instead of taking a dependency is that it must be provably right, not plausibly right.
_FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
_FIPS_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
_FIPS_CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_fips197_encrypt_block() -> None:
    rk = _aes._expand_key(_FIPS_KEY)
    assert _aes._encrypt_block(rk, _FIPS_PLAIN) == _FIPS_CIPHER


def test_fips197_decrypt_block() -> None:
    rk = _aes._expand_key(_FIPS_KEY)
    assert _aes._decrypt_block(rk, _FIPS_CIPHER) == _FIPS_PLAIN


def test_cbc_matches_a_known_vector() -> None:
    """NIST SP 800-38A F.2.1, first block of the CBC-AES128 example."""
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    out = _aes.encrypt(key, iv, plain)
    assert out[:16] == bytes.fromhex("7649abac8119b246cee98e9b12e9197d")


@pytest.mark.parametrize(
    "message",
    [b"", b"a", b"0123456789abcde", b"0123456789abcdef", b"admin\nhunter2", bytes(range(256))],
)
def test_cbc_round_trip(message: bytes) -> None:
    key, iv = b"0123456789abcdef", b"abcdef9876543210"
    assert _aes.decrypt(key, iv, _aes.encrypt(key, iv, message)) == message


def test_exact_block_length_still_gains_padding() -> None:
    """PKCS#7 always adds a block, otherwise unpadding a 16-byte message is ambiguous."""
    key, iv = b"0123456789abcdef", b"abcdef9876543210"
    assert len(_aes.encrypt(key, iv, b"0123456789abcdef")) == 32


def test_garbled_ciphertext_returns_empty_rather_than_raising() -> None:
    """The caller parses whatever a router sent; a bad body must not raise out of the cipher."""
    key, iv = b"0123456789abcdef", b"abcdef9876543210"
    assert _aes.decrypt(key, iv, b"not-a-block-multiple") == b""
    assert _aes.decrypt(key, iv, b"") == b""


def test_bad_padding_is_tolerated() -> None:
    key, iv = b"0123456789abcdef", b"abcdef9876543210"
    # 16 bytes of ciphertext that will not decrypt to anything with valid padding.
    out = _aes.decrypt(key, iv, bytes(16))
    assert isinstance(out, bytes)  # no exception, whatever it contains


@pytest.mark.parametrize(("key", "iv"), [(b"short", b"abcdef9876543210"), (b"0123456789abcdef", b"short")])
def test_wrong_key_or_iv_length_is_a_programming_error(key: bytes, iv: bytes) -> None:
    """Unlike a bad *body*, a bad key/IV is our bug and should be loud."""
    with pytest.raises(ValueError):
        _aes.encrypt(key, iv, b"x")
