"""Encryption at rest for archived logs: envelope encryption, the same shape as Key Vault / KMS (Deliverable 1).

    python -m security.encrypt_at_rest new-kek                      # print a fresh AUDIT_KEK for your .env
    python -m security.encrypt_at_rest encrypt logs/audit.jsonl     # -> logs/audit.jsonl.enc, plaintext kept
    python -m security.encrypt_at_rest decrypt logs/audit.jsonl.enc --out /tmp/audit.jsonl

How it works (and how it maps to the cloud, see security/kms_twin.json):
  1. A fresh 256-bit data key (DEK) is generated for each file.           KMS GenerateDataKey / KV random
  2. The file is encrypted with AES-256-GCM under that DEK.               done locally, as in the cloud
  3. The DEK is wrapped (encrypted) with the key-encryption key (KEK).    KMS Encrypt / Key Vault wrapKey
  4. Only the wrapped DEK is stored, in the file's header, next to the kek_id.
To read a file you need the KEK. In the cloud the KEK never leaves the HSM; here it is AUDIT_KEK, injected
like every other secret (secret_refs.yaml) and never written to disk by this tool. GCM authenticates both
the header and the data, so a flipped bit or a swapped header fails decryption instead of yielding garbage.

What this does NOT cover: the live Docker volume (`triage-logs`) is protected by disk encryption on the host
(Azure managed disk SSE with a customer-managed key, or EBS encrypted with KMS); this tool covers the files
that leave the volume: rotated audit logs going to object storage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"AFYAENC1\n"
ALG = "AES-256-GCM"


class DecryptionError(RuntimeError):
    """Wrong KEK, tampered file, or not an AfyaPlus encrypted file."""


def new_kek() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _kek_bytes(kek_b64: str) -> bytes:
    try:
        raw = base64.b64decode(kek_b64, validate=True)
    except ValueError as exc:
        raise ValueError("AUDIT_KEK must be base64 of 32 bytes (python -m security.encrypt_at_rest new-kek)") from exc
    if len(raw) != 32:
        raise ValueError(f"AUDIT_KEK must decode to 32 bytes, got {len(raw)}")
    return raw


def kek_id(kek_b64: str) -> str:
    """Identifies which KEK wrapped a file (a KMS key ARN / Key Vault key version in the cloud)."""
    return "local-kek-" + hashlib.sha256(_kek_bytes(kek_b64)).hexdigest()[:12]


def encrypt_bytes(plaintext: bytes, kek_b64: str, aad: str = "") -> bytes:
    kek = _kek_bytes(kek_b64)
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce, data_nonce = os.urandom(12), os.urandom(12)
    header = {
        "alg": ALG, "kek_id": kek_id(kek_b64), "aad": aad,
        "wrapped_dek": base64.b64encode(wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, b"dek")).decode(),
        "nonce": base64.b64encode(data_nonce).decode(),
    }
    header_bytes = json.dumps(header, sort_keys=True).encode()
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, header_bytes)  # header is authenticated too
    return MAGIC + header_bytes + b"\n" + ciphertext


def decrypt_bytes(blob: bytes, kek_b64: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise DecryptionError("not an AfyaPlus encrypted file")
    header_bytes, _, ciphertext = blob[len(MAGIC):].partition(b"\n")
    try:
        header = json.loads(header_bytes)
        wrapped = base64.b64decode(header["wrapped_dek"])
        dek = AESGCM(_kek_bytes(kek_b64)).decrypt(wrapped[:12], wrapped[12:], b"dek")
        return AESGCM(dek).decrypt(base64.b64decode(header["nonce"]), ciphertext, header_bytes)
    except Exception as exc:  # noqa: BLE001 - every failure means the same thing to the caller
        raise DecryptionError(f"cannot decrypt: wrong key or tampered file ({type(exc).__name__})") from None


def _kek_from_env() -> str:
    kek = os.environ.get("AUDIT_KEK", "")
    if not kek:
        print("AUDIT_KEK is not set (see security/secret_refs.yaml); refusing to continue", file=sys.stderr)
        sys.exit(1)
    return kek


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Envelope-encrypt files at rest (KMS / Key Vault twin).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new-kek")
    enc = sub.add_parser("encrypt")
    enc.add_argument("path", type=Path)
    dec = sub.add_parser("decrypt")
    dec.add_argument("path", type=Path)
    dec.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "new-kek":
        print(new_kek())
        return 0
    kek = _kek_from_env()
    if args.cmd == "encrypt":
        out = args.path.with_name(args.path.name + ".enc")
        out.write_bytes(encrypt_bytes(args.path.read_bytes(), kek, aad=args.path.name))
        print(f"encrypted {args.path} -> {out} ({ALG}, {kek_id(kek)}, {out.stat().st_size} bytes)")
        return 0
    try:
        args.out.write_bytes(decrypt_bytes(args.path.read_bytes(), kek))
    except DecryptionError as exc:
        print(f"DECRYPT FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"decrypted {args.path} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
