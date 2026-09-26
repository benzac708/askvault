"""Server-rendered web surface. See `app.web.render` for why it is hand-rolled."""

from app.web.render import PLACEHOLDERS, TEMPLATE_PATH, render_page

__all__ = ["PLACEHOLDERS", "TEMPLATE_PATH", "render_page"]
