"""Local PDF conversion; no Feishu, web, or task-store dependency."""

from .parser import DoclingParser, inspect_pdf, render_pages

__all__ = ["DoclingParser", "inspect_pdf", "render_pages"]
