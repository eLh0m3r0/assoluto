"""Per-recipient-email rate limits.

Complements the per-IP throttle in :mod:`app.security.rate_limit`.

Per-IP is effective against a single-source attacker hammering one
endpoint, but it leaves a blind spot: an attacker who rotates source
IPs (cheap VPN exit pools, botnets) can still bombard a single
victim's inbox with password-reset links, re-sent invitations, or
verification emails. Those aren't account-takeover on their own, but
spamming someone's inbox with ten legitimate-looking "reset your
password" mails is a phishing primer.

This module tracks attempts per lower-cased recipient email in a
sliding-window buckets, entirely in-process. A multi-worker deploy
can drop the limit per-worker (which is actually the *upper* bound
across the fleet), but if you need tight shared state switch to
Redis-backed storage.

Usage::

    from app.security.email_throttle import PASSWORD_RESET_THROTTLE
    if not PASSWORD_RESET_THROTTLE.allow(email):
        # silently drop — don't leak that the address is throttled
        return _generic_success_page(request)
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic


class EmailThrottle:
    """Sliding-window per-email limiter.

    ``max_attempts`` within ``window_seconds`` before ``allow()`` starts
    returning ``False``. Entries are kept in a ``deque`` per key; eviction
    happens on access (O(k) amortised). Process-local.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        if max_attempts <= 0 or window_seconds <= 0:
            raise ValueError("max_attempts and window_seconds must be positive")
        self._max = max_attempts
        self._win = float(window_seconds)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, email: str) -> bool:
        """Record a new attempt and return whether it's within the cap."""
        key = (email or "").strip().lower()
        if not key:
            # No email = nothing to throttle on; fall back to per-IP.
            return True
        now = monotonic()
        cutoff = now - self._win
        with self._lock:
            q = self._buckets.get(key)
            if q is None:
                q = deque()
                self._buckets[key] = q
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        """Drop all state. Used by tests."""
        with self._lock:
            self._buckets.clear()


# Singleton throttles, tuned per endpoint. Thresholds are deliberately
# generous for humans ("I typoed the email three times") but tight
# enough that a mass-mail script is caught.

# 5 reset links per email per hour. Legitimate flow needs 1.
PASSWORD_RESET_THROTTLE = EmailThrottle(max_attempts=5, window_seconds=60 * 60)

# 3 invite resends per email per hour. Legitimate flow needs maybe 2.
INVITE_RESEND_THROTTLE = EmailThrottle(max_attempts=3, window_seconds=60 * 60)

# 3 signups per email per day. Legitimate flow needs 1.
SIGNUP_THROTTLE = EmailThrottle(max_attempts=3, window_seconds=60 * 60 * 24)

# 5 verification resends per email per day.
VERIFY_RESEND_THROTTLE = EmailThrottle(max_attempts=5, window_seconds=60 * 60 * 24)
