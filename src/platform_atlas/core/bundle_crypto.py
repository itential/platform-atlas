"""
Encrypted environment-setup bundles.

The browser-based setup wizard (``guides/env-setup.html``) can export the whole
environment — non-sensitive config *and* secrets — as a single encrypted file so
the operator does not have to re-type credentials into CLI prompts.  The file is
encrypted client-side with the Web Crypto API; this module is the matching
decrypt side used by ``env create --from-file``.

Crypto contract (must stay byte-for-byte compatible with the browser):

    KDF     PBKDF2-HMAC-SHA256, ``iterations`` per envelope, 32-byte key
    cipher  AES-256-GCM, 12-byte random IV, 128-bit tag appended to ciphertext
    AAD     the fixed marker below, binding the version into the auth tag
    salt    16 random bytes per file, IV 12 random bytes per encrypt operation

The envelope is plain JSON (all binary fields base64) so it survives email,
shared drives and copy-paste without corruption:

    {
      "_atlas_bundle": "encrypted-v1",
      "kdf": "PBKDF2-SHA256", "iterations": 600000, "cipher": "AES-256-GCM",
      "salt": "<b64>", "iv": "<b64>", "ciphertext": "<b64>"
    }

This is *transport*, not storage: the decrypted secrets go straight into the
configured credential backend, and the bundle is shredded after a successful
import.  Passphrase strength (auto-generated, ~16 chars) plus the slow KDF is the
security boundary — see ``env-encrypted-bundle-design.md``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

# Envelope marker + AAD. The marker is what ``--from-file`` keys off to tell an
# encrypted bundle from a plaintext JSON, and (as AAD) it is authenticated by the
# GCM tag so a version mismatch fails closed rather than decrypting garbage.
BUNDLE_MARKER = "encrypted-v1"
_BUNDLE_AAD = b"atlas-env-bundle-v1"

# 600k PBKDF2-SHA256 iterations: OWASP-current, ~a few hundred ms in-browser.
DEFAULT_ITERATIONS = 600_000

# Sanity bounds on the iteration count read from an (untrusted) envelope. The
# browser always writes DEFAULT_ITERATIONS; this window leaves generous room to
# raise the KDF cost later while refusing a value large enough to hang the
# import (PBKDF2 runs before the auth tag is ever checked, so a hand-crafted
# file could otherwise pin a CPU with no passphrase needed).
#
# The floor matters as much as the ceiling. ``iterations`` is read from the file
# and is NOT covered by the GCM AAD, so a modified generator could emit bundles
# at a token cost — the CLI would import them happily while the passphrase
# protecting SSH keys, Mongo and Vault credentials became trivially brute
# forcible. Refuse anything weaker than the OWASP floor.
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 10_000_000

# Unambiguous passphrase alphabet — no 0/O, 1/l/I. ~16 chars ≈ 90 bits, and the
# slow KDF makes each guess expensive, so this is comfortably strong while still
# being clean to read off screen and re-type.
_PASSPHRASE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
_PASSPHRASE_LENGTH = 16


class BundleError(Exception):
    """A setup bundle could not be read or decrypted."""


class BundleDecryptError(BundleError):
    """Wrong passphrase, or the bundle has been tampered with / corrupted."""


def is_encrypted_bundle(obj: object) -> bool:
    """True if *obj* is a parsed encrypted-bundle envelope."""
    return isinstance(obj, dict) and obj.get("_atlas_bundle") == BUNDLE_MARKER


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 → 32-byte AES-256 key (matches Web Crypto deriveKey)."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def generate_passphrase(length: int = _PASSPHRASE_LENGTH) -> str:
    """
    A cryptographically-random passphrase from the unambiguous alphabet.

    The browser is the authority in the real flow (it generates and displays the
    passphrase at encryption time); this exists for tests, round-tripping and any
    future CLI-side export.
    """
    return "".join(secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(length))


def encrypt_bundle(
    payload: dict,
    passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> dict:
    """
    Encrypt *payload* into an envelope dict (see module docstring).

    Primarily used by tests and to keep both sides of the contract in one place;
    the production encrypt path is the browser.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = _derive_key(passphrase, salt, iterations)
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, _BUNDLE_AAD)
    return {
        "_atlas_bundle": BUNDLE_MARKER,
        "kdf": "PBKDF2-SHA256",
        "iterations": iterations,
        "cipher": "AES-256-GCM",
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_bundle(envelope: dict, passphrase: str) -> dict:
    """
    Decrypt an encrypted-bundle *envelope* using *passphrase*.

    Raises :class:`BundleDecryptError` on a wrong passphrase or a tampered /
    corrupt bundle (indistinguishable by design — AES-GCM auth failure), and
    :class:`BundleError` if the envelope is structurally invalid.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    if not is_encrypted_bundle(envelope):
        raise BundleError("not an Atlas encrypted bundle")
    if (envelope.get("cipher") or "AES-256-GCM") != "AES-256-GCM":
        raise BundleError(f"unsupported cipher: {envelope.get('cipher')!r}")

    try:
        salt = base64.b64decode(envelope["salt"])
        iv = base64.b64decode(envelope["iv"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        iterations = int(envelope.get("iterations", DEFAULT_ITERATIONS))
    except (KeyError, ValueError, TypeError) as exc:
        raise BundleError(f"malformed bundle envelope: {exc}") from exc

    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise BundleError(
            f"bundle iteration count {iterations} is outside the accepted range "
            f"({MIN_ITERATIONS}–{MAX_ITERATIONS})"
        )

    key = _derive_key(passphrase, salt, iterations)
    try:
        plaintext = AESGCM(key).decrypt(iv, ciphertext, _BUNDLE_AAD)
    except InvalidTag as exc:
        raise BundleDecryptError(
            "Could not decrypt — the passphrase is wrong or the file has been altered."
        ) from exc

    try:
        obj = json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleError(f"decrypted content is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise BundleError("decrypted content is not a JSON object")
    return obj
