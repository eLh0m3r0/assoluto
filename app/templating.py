"""Jinja2 template setup.

We use `jinja2-fragments` so that HTMX endpoints can return a named fragment
from a full template without duplicating markup. Non-HTMX requests get the
full page; HTMX requests get only the requested block.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2_fragments import render_block

from app import __version__
from app.config import Settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_jinja_env() -> Environment:
    """Create the project-wide Jinja2 environment."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
        enable_async=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


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
