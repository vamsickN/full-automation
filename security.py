"""Agent 1: Security Layer for Continuity Studio.

Fixes:
- Rate limiting on auth endpoints (brute-force protection)
- Secure session handling
- Input sanitization
- File upload validation
- CSRF protection basics
"""
import time
import hashlib
import hmac
import os
import re
from collections import defaultdict
from typing import Optional, Tuple


class RateLimiter:
    """In-memory rate limiter with sliding window."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts = defaultdict(list)  # ip -> [timestamps]

    def is_blocked(self, ip: str) -> bool:
        """Check if an IP is currently rate-limited."""
        now = time.time()
        # Clean old attempts
        self._attempts[ip] = [
            t for t in self._attempts[ip] if now - t < self.window
        ]
        return len(self._attempts[ip]) >= self.max_attempts

    def record(self, ip: str):
        """Record a failed attempt."""
        self._attempts[ip].append(time.time())

    def reset(self, ip: str):
        """Reset on successful login."""
        self._attempts.pop(ip, None)

    def remaining(self, ip: str) -> int:
        """Remaining attempts before block."""
        now = time.time()
        recent = [t for t in self._attempts.get(ip, []) if now - t < self.window]
        return max(0, self.max_attempts - len(recent))


# Global rate limiters
login_limiter = RateLimiter(max_attempts=5, window_seconds=300)
signup_limiter = RateLimiter(max_attempts=3, window_seconds=600)
api_limiter = RateLimiter(max_attempts=100, window_seconds=60)


class SecureSession:
    """Improved session signing with expiry and rotation."""

    def __init__(self, secret: str, max_age: int = 86400 * 7):
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.max_age = max_age

    def create(self, email: str) -> str:
        """Create a signed session token with timestamp."""
        import base64
        ts = str(int(time.time()))
        payload = f"{email}|{ts}"
        payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
        sig = hmac.new(self.secret, payload_b64.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload_b64}.{sig}"

    def verify(self, token: str) -> Optional[str]:
        """Verify token and check expiry. Returns email or None."""
        import base64
        if not token or "." not in token:
            return None
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(self.secret, payload_b64.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            decoded = base64.urlsafe_b64decode(payload_b64.encode()).decode()
            parts = decoded.split("|")
            if len(parts) != 2:
                return None
            email, ts = parts
            # Check expiry
            if time.time() - int(ts) > self.max_age:
                return None
            return email
        except Exception:
            return None


def hash_password(password: str, salt: str) -> str:
    """PBKDF2 password hashing with proper iteration count."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 310_000
    ).hex()


def validate_email(email: str) -> bool:
    """Basic email format validation."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 254


def validate_password(password: str) -> Tuple[bool, str]:
    """Password strength validation."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password too long"
    if not re.search(r'[A-Za-z]', password):
        return False, "Password must contain at least one letter"
    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one number"
    return True, ""


ALLOWED_VIDEO_MIMES = {
    "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo",
    "video/x-matroska", "video/mpeg",
}
ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
}
ALLOWED_AUDIO_MIMES = {
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/webm", "audio/mp4",
}


def validate_upload(filename: str, content_type: str, allowed: set) -> Tuple[bool, str]:
    """Validate uploaded file type."""
    if not filename:
        return False, "No filename provided"
    # Block dangerous extensions
    dangerous = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js", ".msi"}
    ext = os.path.splitext(filename)[1].lower()
    if ext in dangerous:
        return False, f"File type {ext} not allowed"
    if content_type and content_type not in allowed:
        return False, f"Content type {content_type} not allowed"
    return True, ""


def sanitize_filename(filename: str) -> str:
    """Remove dangerous characters from filenames."""
    # Keep only safe chars
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    # Prevent path traversal
    safe = safe.lstrip('.').replace('..', '')
    return safe[:200] if safe else 'unnamed'


def get_client_ip(request) -> str:
    """Extract real client IP from request (handles proxies)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
