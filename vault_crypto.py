"""Encrypt / decrypt sensitive fields in vault.json.

Key source (in priority order):
  1. CS_SECRET env var  →  derive a Fernet key via PBKDF2
  2. data/.secret file  →  auto-generated Fernet key (created on first run)

If neither is available AND cryptography is missing, falls back to plaintext
with a console warning.

Encrypted values are stored as "enc::<base64-ciphertext>" so they're easy to
distinguish from plaintext during migration.
"""
import base64
import os
import threading

import config

_SENSITIVE_KEYS = {"api_key", "claude_api_key", "elevenlabs_api_key", "anthropic_api_key", "openai_api_key", "ninerouter_api_key", "mimo_api_key", "agentrouter_api_key"}
_ENC_PREFIX = "enc::"
_SECRET_PATH = os.path.join(config.DATA_DIR, ".secret")

_fernet = None
_fallback = False
_INIT_LOCK = threading.Lock()


def _init_fernet():
    global _fernet, _fallback
    if _fernet is not None or _fallback:
        return
    with _INIT_LOCK:
        # Double-checked: another thread may have initialized while we waited.
        if _fernet is not None or _fallback:
            return
        _init_fernet_locked()


def _init_fernet_locked():
    global _fernet, _fallback

    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        print("[vault] WARNING: cryptography not installed — keys stored in PLAINTEXT",
              flush=True)
        _fallback = True
        return

    env_secret = os.environ.get("CS_SECRET", "").strip()
    if env_secret:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=b"continuity-studio-vault", iterations=480_000)
        key = base64.urlsafe_b64encode(kdf.derive(env_secret.encode()))
        _fernet = Fernet(key)
        return

    os.makedirs(config.DATA_DIR, exist_ok=True)
    if os.path.exists(_SECRET_PATH):
        with open(_SECRET_PATH, "rb") as f:
            key = f.read().strip()
        try:
            _fernet = Fernet(key)
            return
        except Exception:
            pass

    key = Fernet.generate_key()
    # Atomic write so a concurrent reader never sees a half-written key file.
    tmp = _SECRET_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(key)
    os.replace(tmp, _SECRET_PATH)
    _fernet = Fernet(key)


def _encrypt(value):
    """Encrypt a string value. Returns enc::-prefixed string."""
    _init_fernet()
    if _fallback or not _fernet or not value:
        return value
    ct = _fernet.encrypt(value.encode())
    return _ENC_PREFIX + ct.decode()


def _decrypt(value):
    """Decrypt an enc::-prefixed value. Returns plaintext."""
    _init_fernet()
    if not value or not isinstance(value, str):
        return value
    if not value.startswith(_ENC_PREFIX):
        return value
    if _fallback or not _fernet:
        return value
    try:
        return _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        print("[vault] WARNING: decryption failed — key may have changed", flush=True)
        return ""


def encrypt_vault(vault):
    """Return a copy of vault with sensitive fields encrypted."""
    out = {}
    for email, settings in vault.items():
        s = dict(settings)
        for k in _SENSITIVE_KEYS:
            v = s.get(k, "")
            if v and not v.startswith(_ENC_PREFIX):
                s[k] = _encrypt(v)
        out[email] = s
    return out


def decrypt_vault(vault):
    """Return a copy of vault with sensitive fields decrypted."""
    out = {}
    for email, settings in vault.items():
        s = dict(settings)
        for k in _SENSITIVE_KEYS:
            v = s.get(k, "")
            if v:
                s[k] = _decrypt(v)
        out[email] = s
    return out


def is_encrypted():
    """True if encryption is active (not fallback plaintext)."""
    _init_fernet()
    return _fernet is not None and not _fallback


def needs_migration(vault):
    """True if any sensitive field is plaintext (not enc:: prefixed)."""
    for settings in vault.values():
        for k in _SENSITIVE_KEYS:
            v = settings.get(k, "")
            if v and not v.startswith(_ENC_PREFIX):
                return True
    return False
