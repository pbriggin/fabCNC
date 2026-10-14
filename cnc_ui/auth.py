"""Local UI password and permanent factory-recovery credential handling."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import base64
from pathlib import Path


AUTH_CONFIG_PATH = Path(__file__).resolve().parent / "auth_config.json"
RECOVERY_CONFIG_PATH = Path(__file__).resolve().parent / "recovery_config.json"
_ITERATIONS = 600_000


def _derive(password: str, salt: bytes, iterations: int = _ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def verify_password(password: str, default_password: str) -> bool:
    """Verify against the saved hash, or the configured default before first change."""
    try:
        payload = json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
        salt = bytes.fromhex(payload["salt"])
        expected = bytes.fromhex(payload["hash"])
        iterations = int(payload.get("iterations", _ITERATIONS))
        return hmac.compare_digest(_derive(password, salt, iterations), expected)
    except FileNotFoundError:
        return hmac.compare_digest(password, default_password)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        # A damaged credential file must never fall back to the known default.
        return False


def set_password(new_password: str) -> None:
    """Persist a salted PBKDF2 hash using an atomic same-directory replacement."""
    if not 4 <= len(new_password) <= 128:
        raise ValueError("Password must be between 4 and 128 characters")
    salt = secrets.token_bytes(32)
    payload = {
        "version": 1,
        "algorithm": "pbkdf2_sha256",
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(new_password, salt).hex(),
    }
    AUTH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTH_CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(AUTH_CONFIG_PATH)


def _normalize_recovery_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


def _write_hash(path: Path, secret: str) -> None:
    salt = secrets.token_bytes(32)
    payload = {
        "version": 1,
        "algorithm": "pbkdf2_sha256",
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(secret, salt).hex(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def provision_recovery_code() -> str | None:
    """Create the machine's permanent recovery code once and return it once."""
    if RECOVERY_CONFIG_PATH.exists():
        return None
    # 120 random bits produce 24 Base32 characters: strong, unambiguous, and
    # evenly split into short groups for a printed label.
    raw = base64.b32encode(secrets.token_bytes(15)).decode("ascii").rstrip("=")
    code = "-".join(raw[index:index + 5] for index in range(0, len(raw), 5))
    _write_hash(RECOVERY_CONFIG_PATH, _normalize_recovery_code(code))
    return code


def verify_recovery_code(code: str) -> bool:
    """Verify a factory recovery code without ever storing its plaintext."""
    normalized = _normalize_recovery_code(code)
    if not normalized:
        return False
    try:
        payload = json.loads(RECOVERY_CONFIG_PATH.read_text(encoding="utf-8"))
        salt = bytes.fromhex(payload["salt"])
        expected = bytes.fromhex(payload["hash"])
        iterations = int(payload.get("iterations", _ITERATIONS))
        return hmac.compare_digest(_derive(normalized, salt, iterations), expected)
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "provision-recovery":
        generated = provision_recovery_code()
        if generated:
            print(generated)
        else:
            print("Recovery code already provisioned; existing plaintext cannot be recovered.", file=sys.stderr)
    else:
        print("Usage: auth.py provision-recovery", file=sys.stderr)
        raise SystemExit(2)

