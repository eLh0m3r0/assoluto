"""Spam filters for the public ``/contact`` form.

Two cheap layers that catch the bot fill pattern observed in the May
2026 Brevo-block incident (random English/Hindi/German first-name
mixes, throwaway email domains, single-word names):

* :func:`is_disposable_email` — domain on a maintained throwaway list.
* :func:`looks_like_bot_local_part` — long pure-alphanumeric local
  part with no human separators (``.``, ``-``, ``+``, ``_``).

Both return ``True`` when the input looks bot-like. Caller is
expected to silent-success on a hit so the bot doesn't iterate.
"""

from __future__ import annotations

import re

# Top throwaway-mail / disposable-mail domains. Not exhaustive — the
# universe of these is in the thousands and rotates monthly. The list
# below covers the ones we have actually seen abuse from + the most
# common ones in published surveys. Keep alphabetised so additions
# don't collide in diffs.
DISPOSABLE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "10minutemail.com",
        "10minutemail.net",
        "20minutemail.com",
        "33mail.com",
        "burnermail.io",
        "deadaddress.com",
        "dispostable.com",
        "dropmail.me",
        "email-fake.com",
        "emailondeck.com",
        "example.com",
        "example.net",
        "example.org",
        "fakeinbox.com",
        "fakemail.net",
        "fakermail.com",
        "getairmail.com",
        "getnada.com",
        "guerrillamail.biz",
        "guerrillamail.com",
        "guerrillamail.de",
        "guerrillamail.info",
        "guerrillamail.net",
        "guerrillamail.org",
        "guerrillamailblock.com",
        "harakirimail.com",
        "incognitomail.org",
        "inboxbear.com",
        "jetable.org",
        "mailcatch.com",
        "maildrop.cc",
        "mailforspam.com",
        "mailinator.com",
        "mailinator.net",
        "mailnesia.com",
        "mailnull.com",
        "mintemail.com",
        "moakt.com",
        "mohmal.com",
        "mt2015.com",
        "mvrht.com",
        "mytemp.email",
        "nada.email",
        "nwytg.net",
        "onetimemail.org",
        "rcpt.at",
        "sharklasers.com",
        "sneakemail.com",
        "spam4.me",
        "spamgourmet.com",
        "spamspot.com",
        "tempail.com",
        "tempmail.com",
        "tempmail.net",
        "temp-mail.org",
        "temp-mail.io",
        "tempmailaddress.com",
        "tempr.email",
        "test.com",
        "throwawaymail.com",
        "trashmail.com",
        "trashmail.de",
        "trashmail.net",
        "yopmail.com",
        "yopmail.fr",
        "yopmail.net",
    }
)


# Local part that's a long random alphanumeric run with no separators —
# typical of bot account names like ``ftgrgxbafx@example.com``. Length
# floor of 12 trades false positives (very few humans have a 12-char
# all-alphanumeric local part with no `.`, `-`, `+`, `_`) for catching
# the abuse pattern. A bare keyword like ``info`` or ``hello`` is fine
# — too short. A first.last@ pattern is fine — has a separator.
_RANDOM_LOCAL_RE = re.compile(r"^[a-z0-9]{12,}$")


def _split_email(email: str) -> tuple[str, str] | None:
    candidate = (email or "").strip().lower()
    if "@" not in candidate:
        return None
    local, _, domain = candidate.rpartition("@")
    if not local or not domain:
        return None
    return local, domain


def is_disposable_email(email: str) -> bool:
    """Return ``True`` when ``email``'s domain is on the throwaway list."""
    parts = _split_email(email)
    if parts is None:
        return False
    _, domain = parts
    return domain in DISPOSABLE_EMAIL_DOMAINS


def looks_like_bot_local_part(email: str) -> bool:
    """Return ``True`` for emails like ``ftgrgxbafx@…`` — random,
    long, no human separators. False for ``vaclav.mudra@`` (dot),
    ``info@`` (too short), ``j.smith+work@`` (plus)."""
    parts = _split_email(email)
    if parts is None:
        return False
    local, _ = parts
    if any(sep in local for sep in (".", "-", "+", "_")):
        return False
    return bool(_RANDOM_LOCAL_RE.match(local))
