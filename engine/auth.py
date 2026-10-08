"""One shared password for the web UI, and signed session cookies.

The password is stored as an scrypt hash in the config ("scrypt$n$r$p$salt$hash",
base64 parts). Sessions are "<expiry>.<hmac>" signed with a random secret kept
in the config, so they survive restarts and a password change can revoke them
all by rotating the secret.
"""

import base64
import hashlib
import hmac
import secrets
import time

N, R, P = 2 ** 14, 8, 1
SESSION_DAYS = 30
MIN_PASSWORD = 6


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(h)}"


def check_password(password, stored):
    try:
        kind, n, r, p, salt, h = stored.split("$")
        if kind != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, _unb64(h))
    except (ValueError, AttributeError):
        return False


def new_secret():
    return secrets.token_hex(32)


def make_session(secret, days=SESSION_DAYS):
    expiry = str(int(time.time() + days * 86400))
    sig = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def check_session(token, secret):
    if not token or not secret or "." not in token:
        return False
    expiry, sig = token.split(".", 1)
    good = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


def set_password(cfg, password):
    """Set the password and rotate the session secret (logs everyone out)."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
    cfg["auth"]["password_hash"] = hash_password(password)
    cfg["auth"]["session_secret"] = new_secret()
    return cfg
