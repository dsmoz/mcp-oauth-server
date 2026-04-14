import hashlib
import secrets
import time
import bcrypt


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def generate_client_id() -> str:
    return "mc_" + secrets.token_urlsafe(16)


def generate_user_id() -> str:
    return "usr_" + secrets.token_urlsafe(12)


def hash_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def verify_secret(secret: str, hashed: str) -> bool:
    return bcrypt.checkpw(secret.encode(), hashed.encode())


def now_unix() -> int:
    return int(time.time())


def hash_token(token: str) -> str:
    """SHA-256 hash of a token for DB storage. Never store raw tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


def compute_dcr_fingerprint(client_name: str, redirect_uris: list[str]) -> str | None:
    """Deterministic fingerprint for DCR dedup. Returns None to skip dedup for generic clients."""
    name = client_name.strip().lower()
    if name == "mcp client" and not redirect_uris:
        return None
    uris = sorted([u.strip().rstrip("/").lower() for u in redirect_uris])
    raw = f"{name}|{','.join(uris)}"
    return hashlib.sha256(raw.encode()).hexdigest()
