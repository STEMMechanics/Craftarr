"""Shared HTML layout for Craftarr email messages."""

import re
from html import escape

from .settings_manager import get_instance_settings


_URL_PATTERN = re.compile(r"https?://[^\s<>]+")


def _linkify_line(line: str) -> str:
    pieces = []
    position = 0
    for match in _URL_PATTERN.finditer(line):
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        pieces.append(escape(line[position:match.start()]))
        if url:
            safe_url = escape(url, quote=True)
            pieces.append(
                f'<a href="{safe_url}" style="color:#167c68;text-decoration:underline;word-break:break-all">'
                f'{escape(url)}</a>'
            )
        pieces.append(escape(trailing))
        position = match.end()
    pieces.append(escape(line[position:]))
    return "".join(pieces)


def format_plain_body(body: str) -> str:
    """Turn plain-text paragraphs into safe HTML while keeping URLs clickable."""
    blocks = re.split(r"\n[ \t]*\n", body.strip())
    paragraphs = []
    for block in blocks:
        lines = block.splitlines() or [block]
        content = "<br>".join(_linkify_line(line) for line in lines)
        paragraphs.append(
            f'<p style="margin:0 0 13px;color:#405c6d;font-size:14px;line-height:1.65">{content}</p>'
        )
    return "".join(paragraphs)


def render_branded_email(
    db,
    title: str,
    content_html: str,
    *,
    eyebrow: str = "Craftarr",
) -> str:
    """Wrap message content in the shared Craftarr header and footer."""
    settings = get_instance_settings(db)
    instance_name = escape(settings["instance_name"])
    public_url = settings["public_url"]
    logo_src = (
        escape(f"{public_url}/static/images/logo.png", quote=True)
        if public_url
        else "cid:craftarr-logo"
    )
    title = escape(title)
    eyebrow = escape(eyebrow)
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f2f6f8;font-family:Arial,Helvetica,sans-serif;color:#233b4d">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2f6f8"><tr><td align="center" style="padding:28px 14px">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px">'
        '<tr><td style="padding:14px 22px;background:#ffffff;border-radius:12px 12px 0 0">'
        f'<img src="{logo_src}" width="220" alt="Craftarr" style="display:block;width:220px;max-width:100%;height:auto;border:0">'
        '</td></tr>'
        '<tr><td style="padding:21px 24px;background:#123846">'
        f'<div style="color:#91dfca;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase">{eyebrow}</div>'
        f'<h1 style="margin:8px 0 5px;color:#ffffff;font-size:25px;line-height:1.25">{title}</h1>'
        f'<p style="margin:0;color:#d0e2e7;font-size:14px">{instance_name}</p>'
        '</td></tr>'
        f'<tr><td style="padding:21px 0 0">{content_html}</td></tr>'
        '<tr><td style="padding:14px 3px 0;color:#718697;font-size:12px;line-height:1.6">Sent by Craftarr</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def render_plain_email(db, subject: str, body: str) -> str:
    """Render an ordinary text message inside the shared Craftarr email layout."""
    message_card = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dce7ed;border-radius:10px;background:#ffffff">'
        f'<tr><td style="padding:19px 20px">{format_plain_body(body)}</td></tr></table>'
    )
    return render_branded_email(db, subject, message_card, eyebrow="Craftarr message")
