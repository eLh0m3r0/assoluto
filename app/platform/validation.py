"""Self-signup form validators.

Contains the reserved slug blocklist and a normaliser that turns a free-text
company name into a safe subdomain slug. Kept separate from
``platform/service.py`` so it can be unit-tested without a DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from slugify import slugify

# Minimum length for the tenant slug. Too short = collision-prone + bad UX.
SLUG_MIN_LEN = 3
SLUG_MAX_LEN = 40

# Reserved words that must never become tenant slugs. Includes our own
# hostnames, generic DNS/marketing names, and anything that could create
# a nasty phishing surface (``admin``, ``support`` etc.).
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        # Own namespaces
        "www",
        "api",
        "app",
        "portal",
        "platform",
        "admin",
        "static",
        "assets",
        "cdn",
        "docs",
        "blog",
        "status",
        "help",
        "support",
        "dashboard",
        # Marketing routes we might add later
        "features",
        "pricing",
        "contact",
        "about",
        "terms",
        "privacy",
        "legal",
        "login",
        "signup",
        "register",
        "signin",
        "logout",
        "auth",
        # Auth / infra prefixes to avoid phishing
        "mail",
        "smtp",
        "imap",
        "pop",
        "ns",
        "ns1",
        "ns2",
        "dns",
        "vpn",
        "ftp",
        "git",
        # Common tenancy / billing terms
        "billing",
        "invoices",
        "billing-admin",
        "plans",
        "settings",
        "account",
        "accounts",
        "profile",
        "me",
        # Developer namespaces
        "test",
        "demo",
        "staging",
        "dev",
        "local",
        "localhost",
        "beta",
    }
)

# RFC 1035-ish: lowercase letters, digits, single hyphens (no leading/trailing).
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class SignupForm:
    """Parsed + normalised signup form values."""

    company_name: str
    slug: str
    owner_email: str
    owner_full_name: str
    password: str
    terms_accepted: bool


class SignupValidationError(Exception):
    """Raised when a signup form is invalid.

    Carries a `field` attribute so the caller can render the error next
    to the right input, and a human-readable message in Czech (the
    signup page is bilingual via gettext).
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def normalise_slug(raw: str) -> str:
    """Convert free-text to a DNS-safe slug candidate.

    Never raises — returns the best-effort normalisation. Callers must
    still run the result through :func:`validate_slug`.
    """
    return slugify(raw or "", lowercase=True, max_length=SLUG_MAX_LEN, regex_pattern=r"[^a-z0-9-]+")


def validate_slug(slug: str) -> str:
    """Return the slug if valid, raise :class:`SignupValidationError` otherwise."""
    if not slug:
        raise SignupValidationError("slug", "Subdoména je povinná.")
    if len(slug) < SLUG_MIN_LEN:
        raise SignupValidationError("slug", f"Subdoména musí mít alespoň {SLUG_MIN_LEN} znaky.")
    if len(slug) > SLUG_MAX_LEN:
        raise SignupValidationError("slug", f"Subdoména může mít nejvýše {SLUG_MAX_LEN} znaků.")
    if not _SLUG_RE.fullmatch(slug):
        raise SignupValidationError(
            "slug",
            "Subdoména smí obsahovat jen malá písmena, číslice a pomlčky.",
        )
    if slug in RESERVED_SLUGS:
        raise SignupValidationError("slug", "Tato subdoména je rezervovaná.")
    return slug


def validate_password(password: str, *, user_inputs: list[str] | None = None) -> str:
    """Validate signup passwords.

    Length rule keeps the DB column bounded. The zxcvbn score guards
    against trivially guessable choices ("password123", "qwerty",
    email-derived passwords, etc.). Score is 0-4; we require ≥ 2
    (OWASP ASVS L1 floor).

    ``user_inputs`` lets zxcvbn down-weight tokens the user already
    supplied elsewhere in the form (email, company name) — "ACME2026"
    is a weak password for someone registering "ACME s.r.o.".
    """
    if len(password) < 8:
        raise SignupValidationError("password", "Heslo musí mít alespoň 8 znaků.")
    if len(password) > 200:
        raise SignupValidationError("password", "Heslo je příliš dlouhé.")
    # Reject leading/trailing whitespace and control characters outright —
    # these usually indicate a paste accident and dramatically weaken the
    # effective entropy we give zxcvbn to score.
    if password.strip() != password:
        raise SignupValidationError(
            "password",
            "Heslo nesmí začínat ani končit mezerou.",
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise SignupValidationError(
            "password",
            "Heslo nesmí obsahovat řídicí znaky.",
        )

    # zxcvbn import is lazy so the dependency stays optional at module
    # load time and the 400 KB frequency dictionary isn't paid for on
    # every request that doesn't validate a password.
    from zxcvbn import zxcvbn

    result = zxcvbn(password, user_inputs=user_inputs or [])
    score = int(result.get("score", 0))
    if score < 2:
        # Surface the zxcvbn suggestion when present; these are well-known
        # short English strings (e.g. "Use a few words, avoid common
        # phrases") — we prepend a Czech framing so users understand.
        feedback = result.get("feedback", {}) or {}
        warning = feedback.get("warning") or "Zvolte silnější heslo."
        raise SignupValidationError(
            "password",
            f"Heslo je příliš slabé. {warning}",
        )
    return password


def validate_email(email: str) -> str:
    email = email.strip().lower()
    # Use email-validator lazily to avoid importing at module load time.
    from email_validator import EmailNotValidError
    from email_validator import validate_email as _ve

    try:
        info = _ve(email, check_deliverability=False)
    except EmailNotValidError as exc:
        raise SignupValidationError("email", str(exc)) from exc
    return info.normalized


def parse_signup_form(
    *,
    company_name: str,
    slug: str,
    owner_email: str,
    owner_full_name: str,
    password: str,
    terms_accepted: bool,
) -> SignupForm:
    """Validate every field; normalise where possible."""
    company_name = (company_name or "").strip()
    if len(company_name) < 2:
        raise SignupValidationError("company_name", "Název firmy je povinný.")

    # If the user left the slug blank, derive one from the company name.
    slug_candidate = (slug or "").strip().lower() or normalise_slug(company_name)
    validated_slug = validate_slug(slug_candidate)
    validated_email = validate_email(owner_email)
    # Feed user-supplied context into zxcvbn so passwords derived from
    # the signup form itself score lower.
    validated_password = validate_password(
        password or "",
        user_inputs=[
            company_name,
            validated_slug,
            validated_email,
            validated_email.split("@", 1)[0],
        ],
    )

    full_name = (owner_full_name or "").strip() or validated_email.split("@", 1)[0]

    if not terms_accepted:
        raise SignupValidationError(
            "terms_accepted", "Musíte potvrdit souhlas s podmínkami služby."
        )

    return SignupForm(
        company_name=company_name,
        slug=validated_slug,
        owner_email=validated_email,
        owner_full_name=full_name,
        password=validated_password,
        terms_accepted=True,
    )
