"""Billing subsystem for the hosted SaaS layer.

Safe to ignore in self-hosted builds — the only reference to this package
is the optional import inside ``app/platform/__init__.py:install()`` which
only runs when ``FEATURE_PLATFORM=true``.

Two modes of operation:

* **Demo mode** (``STRIPE_SECRET_KEY`` unset): billing tables track
  subscriptions locally, ``create_checkout_session()`` returns a
  placeholder URL, and webhooks are a no-op. Good enough for local
  development and for early pilot deployments where you aren't charging
  yet.
* **Live mode** (``STRIPE_SECRET_KEY`` set): we actually talk to Stripe
  for Checkout sessions, Customer Portal, and webhook verification.
"""
