"""Static msgid registry for dynamically-labelled strings.

Several places in the codebase pass *raw* English message IDs through
variables (e.g. `TRANSITION_META["label"]` in `app/routers/orders.py`,
`status_choices` tuples, or dict values rendered as `{{ _(label) }}`
in templates). ``pybabel extract`` can only see *literal* strings
passed to ``_()`` / ``gettext()`` — variables are invisible to it.

Without this file, each extract cycle marks those msgids obsolete
(``#~``), the compiled ``.mo`` drops them, and the UI loses the Czech
translations even though the template is perfectly wrapped.

Each line below is a single ``_()`` call that babel discovers. The
result is discarded immediately — the only side effect is registering
the msgid in ``messages.pot`` so the CS catalog keeps its translation.

When you add a new dynamic label somewhere (a new `OrderStatus`
transition, a new filter choice, a new audit-entity constant), also
add its English msgid here.
"""

from __future__ import annotations


def _(msg: str) -> str:
    """Identity — babel only cares about the literal arguments."""
    return msg


# --------------------------------------------------------------- order
# Status names from ``app/routers/orders.py`` (``status_choices``,
# ``bulk_status_choices``) and the ``OrderStatus`` badge in the status
# filter dropdown.
_("All statuses")
_("Draft")
_("Submitted")
_("Quoted")
_("Confirmed")
_("In production")
_("Ready")
_("Delivered")
_("Closed")
_("Cancelled")

# Transition button labels from ``TRANSITION_META``.
_("Return to draft")
_("Submit")
_("Quote")
_("Confirm")
_("Start production")
_("Close")
_("Cancel")

# --------------------------------------------------------------- audit
# Entity-type filter choices in the audit-log dropdown.
_("All entities")
_("Order")
_("Customer")
_("Product")
_("User")
_("Customer contact")
