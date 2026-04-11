"""Jinja2 template setup.

We use `jinja2-fragments` so that HTMX endpoints can return a named fragment
from a full template without duplicating markup. Non-HTMX requests get the
full page; HTMX requests get only the requested block.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2_fragments import render_block

from app import __version__
from app.config import Settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _qty_filter(value: Any) -> str:
    """Render a Decimal/number without trailing zeros.

    Examples:
        Decimal('75.000') -> '75'
        Decimal('7.500')  -> '7.5'
        Decimal('0')      -> '0'
        None              -> ''
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # Normalize but guard against scientific notation that
        # Decimal.normalize() can produce for large integers.
        normalized = value.normalize()
        _sign, _digits, exponent = normalized.as_tuple()
        if isinstance(exponent, int) and exponent > 0:
            normalized = normalized.quantize(Decimal(1))
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def build_jinja_env() -> Environment:
    """Create the project-wide Jinja2 environment."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        enable_async=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["qty"] = _qty_filter
    return env


class Templates:
    """Thin wrapper exposing `render()` and `render_block()` with app context.

    Mirrors the ergonomics of Starlette's `Jinja2Templates` but goes through
    our own environment so `jinja2-fragments` can share it.
    """

    def __init__(self, env: Environment, settings: Settings) -> None:
        self.env = env
        self.settings = settings

    def _base_context(self, request: Request, extra: dict | None = None) -> dict:
        context: dict = {
            "request": request,
            "app_version": __version__,
            "app_env": self.settings.app_env,
            "url_for": request.url_for,
        }
        if extra:
            context.update(extra)
        return context

    def render(
        self,
        request: Request,
        template_name: str,
        context: dict | None = None,
    ) -> str:
        """Render a full template to a string."""
        template = self.env.get_template(template_name)
        return template.render(self._base_context(request, context))

    def render_block(
        self,
        request: Request,
        template_name: str,
        block_name: str,
        context: dict | None = None,
    ) -> str:
        """Render a single named block from a template (for HTMX fragments)."""
        return render_block(
            self.env,
            template_name,
            block_name,
            **self._base_context(request, context),
        )
