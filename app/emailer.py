import smtplib
from pathlib import Path
from uuid import uuid4

from email.message import EmailMessage

from .email_templates import render_plain_email
from .settings_manager import (
    get_smtp_settings,
)


def send_email(
    db,
    to_address: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
):

    settings = get_smtp_settings(
        db
    )

    host = settings[
        "smtp_host"
    ].strip()

    if not host:
        raise RuntimeError(
            "SMTP is not configured"
        )

    port = int(
        settings[
            "smtp_port"
        ]
        or 587
    )

    username = settings[
        "smtp_username"
    ]

    password = settings[
        "smtp_password"
    ]

    security = settings[
        "smtp_security"
    ]

    from_name = settings[
        "smtp_from_name"
    ]

    from_address = settings[
        "smtp_from_address"
    ].strip()

    if not from_address:
        raise RuntimeError(
            "SMTP From Address is required"
        )


    message = EmailMessage()

    message["Subject"] = subject

    message["From"] = (
        f"{from_name} <{from_address}>"
        if from_name
        else from_address
    )

    message["To"] = to_address

    message.set_content(body)
    html_body = html_body or render_plain_email(db, subject, body)
    logo_path = Path(__file__).resolve().parent / "static" / "images" / "logo.png"
    logo_cid = None
    if logo_path.is_file() and 'src="cid:craftarr-logo"' in html_body:
        logo_cid = f"craftarr-logo-{uuid4().hex}@craftarr.local"
        html_body = html_body.replace(
            'src="cid:craftarr-logo"',
            f'src="cid:{logo_cid}"',
        )
    message.add_alternative(html_body, subtype="html")
    if logo_cid:
        html_part = message.get_payload()[-1]
        html_part.add_related(
            logo_path.read_bytes(),
            maintype="image",
            subtype="png",
            cid=f"<{logo_cid}>",
            filename="craftarr-logo.png",
            disposition="inline",
        )


    if security == "ssl":

        server = smtplib.SMTP_SSL(
            host,
            port,
            timeout=20,
        )

    else:

        server = smtplib.SMTP(
            host,
            port,
            timeout=20,
        )


    try:

        server.ehlo()

        if security == "starttls":

            server.starttls()

            server.ehlo()


        if username:

            server.login(
                username,
                password,
            )


        server.send_message(
            message
        )

    finally:

        server.quit()
