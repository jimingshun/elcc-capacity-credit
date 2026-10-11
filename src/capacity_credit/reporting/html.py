"""T24-A: single-file offline HTML renderer (jinja2, autoescape).

Consumes ONLY the shared ReportView. The output is UTF-8, self-contained
(CSS embedded, strict CSP, no JavaScript, no CDN/fonts/remote images),
escapes every text field, exposes only relative artifact names and never
embeds the full large risk-event set (bounded by the view).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from capacity_credit.reporting.report_model import ReportView

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ENVIRONMENT = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,  # every text field escaped - template ext is .j2
)


def render_html(view: ReportView, path: str | None = None) -> bytes:
    """Render the offline HTML report from the shared view."""
    template = _ENVIRONMENT.get_template("report.html.j2")
    html = template.render(view=view)
    data = html.encode("utf-8")
    if path is not None:
        Path(path).write_bytes(data)
    return data


__all__ = ["render_html"]
