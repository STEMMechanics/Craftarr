"""Render branded update notices with links back to the matching server page."""

from html import escape

from .email_templates import render_branded_email
from .settings_manager import get_instance_settings


def render_update_email(db, updates: list[dict]) -> tuple[str, str, str]:
    settings = get_instance_settings(db)
    instance_name = settings["instance_name"]
    public_url = settings["public_url"]

    grouped: dict[tuple[str, str, str], dict] = {}
    for item in updates:
        node_name = str(item.get("node") or "").strip()
        server_name = str(item.get("server") or "Server")
        path = str(item.get("path") or "")
        key = (node_name, server_name, path)
        group = grouped.setdefault(key, {"node": node_name, "server": server_name, "path": path, "items": []})
        group["items"].append({
            "name": str(item.get("name") or "Update"),
            "installed": str(item.get("installed") or "Unknown"),
            "available": str(item.get("available") or "New version"),
        })

    plain_lines = [
        f"Updates are ready to review · {instance_name}",
        "No updates have been installed automatically.",
        "",
    ]
    cards = []
    for group in grouped.values():
        target = f"{public_url}{group['path']}" if public_url and group["path"] else ""
        plain_lines.append(f"{group['node'] + ' · ' if group['node'] else ''}{group['server']}")
        for item in group["items"]:
            plain_lines.append(f"{item['name']}: {item['installed']} → {item['available']}")
        if target:
            plain_lines.append(f"Review in Craftarr: {target}")
        else:
            plain_lines.append(f"Open {instance_name}, select {group['server']}, then review its updates in Craftarr.")
        plain_lines.append("")

        node_line = (
            f'<div style="margin:0 0 8px;color:#527084;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase">'
            f'{escape(group["node"])} · {escape(instance_name)}</div>'
            if group["node"] else
            f'<div style="margin:0 0 8px;color:#527084;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase">{escape(instance_name)}</div>'
        )
        version_rows = "".join(
            '<tr>'
            f'<td style="padding:9px 10px;border-top:1px solid #e5edf2;color:#233b4d;font-size:14px;font-weight:700">{escape(item["name"])}</td>'
            f'<td style="padding:9px 10px;border-top:1px solid #e5edf2;color:#567084;font-size:13px;text-align:right">{escape(item["installed"])} &nbsp;→&nbsp; <strong style="color:#167c68">{escape(item["available"])}</strong></td>'
            '</tr>'
            for item in group["items"]
        )
        button = (
            f'<a href="{escape(target, quote=True)}" style="display:inline-block;margin-top:17px;padding:11px 17px;border-radius:7px;background:#167c68;color:#ffffff;font-size:14px;font-weight:700;text-decoration:none">Review in Craftarr</a>'
            if target else
            f'<p style="margin:16px 0 0;color:#567084;font-size:13px">Open {escape(instance_name)}, select {escape(group["server"])}, then review its updates in Craftarr.</p>'
        )
        cards.append(
            '<tr><td style="padding:0 0 14px">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dce7ed;border-radius:10px;background:#ffffff">'
            f'<tr><td style="padding:18px 20px">{node_line}'
            f'<div style="margin:0 0 13px;color:#152e40;font-size:19px;font-weight:700">{escape(group["server"])}</div>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
            f'{version_rows}'
            f'</table>{button}</td></tr></table>'
            '</td></tr>'
        )

    content_html = (
        '<p style="margin:0 0 15px;color:#405c6d;font-size:14px;line-height:1.6">The following updates are available. No updates have been installed automatically.</p>'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(cards)}</table>'
        '<p style="margin:7px 0 0;color:#718697;font-size:12px;line-height:1.6">You can review each update before installing it.</p>'
    )
    html_body = render_branded_email(
        db,
        "Updates are ready to review",
        content_html,
        eyebrow="Craftarr update notice",
    )
    subject = f"Craftarr · {instance_name} · Updates available"
    return subject, "\n".join(plain_lines).rstrip(), html_body
