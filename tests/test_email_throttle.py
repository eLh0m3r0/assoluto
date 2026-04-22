"""Unit tests for the per-email rate limiter."""

from __future__ import annotations

import time

from app.security.email_throttle import EmailThrottle


def test_allows_within_limit() -> None:
    t = EmailThrottle(max_attempts=3, window_seconds=60)
    for _ in range(3):
        assert t.allow("x@example.com") is True


def test_rejects_once_exhausted() -> None:
    t = EmailThrottle(max_attempts=3, window_seconds=60)
    for _ in range(3):
        t.allow("x@example.com")
    assert t.allow("x@example.com") is False


def test_case_and_whitespace_insensitive() -> None:
    """Attackers can't escape by capitalising or padding the email."""
    t = EmailThrottle(max_attempts=2, window_seconds=60)
    assert t.allow("x@example.com") is True
    assert t.allow(" X@Example.Com ") is True
    assert t.allow("X@EXAMPLE.COM") is False


def test_empty_email_falls_through() -> None:
    """``""`` / None can't be throttled on, caller must also apply per-IP."""
    t = EmailThrottle(max_attempts=1, window_seconds=60)
    assert t.allow("") is True
    assert t.allow("") is True  # No throttle on blank key.


def test_window_eviction() -> None:
    """Old attempts drop off as the window slides."""
    t = EmailThrottle(max_attempts=2, window_seconds=1)
    assert t.allow("x@example.com") is True
    assert t.allow("x@example.com") is True
    assert t.allow("x@example.com") is False
    time.sleep(1.1)
    assert t.allow("x@example.com") is True


def test_independent_emails() -> None:
    t = EmailThrottle(max_attempts=1, window_seconds=60)
    assert t.allow("a@example.com") is True
    assert t.allow("b@example.com") is True
    assert t.allow("a@example.com") is False


def test_reset_for_tests() -> None:
    t = EmailThrottle(max_attempts=1, window_seconds=60)
    t.allow("x@example.com")
    assert t.allow("x@example.com") is False
    t.reset()
    assert t.allow("x@example.com") is True


def test_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        EmailThrottle(max_attempts=0, window_seconds=10)
    with pytest.raises(ValueError):
        EmailThrottle(max_attempts=3, window_seconds=0)


def test_stale_buckets_evicted_over_time() -> None:
    """Long-running process → unique-email streams → no unbounded growth."""
    t = EmailThrottle(max_attempts=5, window_seconds=1)
    # Force frequent sweeps for the test.
    t._EVICT_EVERY_N = 10
    for i in range(200):
        t.allow(f"user-{i}@ex.com")
    # 200 unique keys; the sweep should have run at least once.
    # Wait long enough that every key is now "stale".
    time.sleep(1.1 * t._EVICT_AFTER_WINDOWS)
    # Force another sweep.
    for i in range(20):
        t.allow(f"fresh-{i}@ex.com")
    # Only the fresh keys should remain.
    # (Plus possibly a handful from the most recent batch.)
    assert len(t._buckets) <= 30, f"expected stale eviction, got {len(t._buckets)}"
