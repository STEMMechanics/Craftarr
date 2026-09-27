import base64
import hashlib
import io
import secrets

import pyotp
import qrcode

from datetime import datetime

from sqlalchemy.orm import Session

from .models import (
    RecoveryCode,
    User,
)


ISSUER_NAME = "Craftarr"


def generate_totp_secret() -> str:

    return pyotp.random_base32()


def provisioning_uri(
    user: User,
    secret: str,
) -> str:

    totp = pyotp.TOTP(
        secret
    )

    return totp.provisioning_uri(
        name=user.username,
        issuer_name=ISSUER_NAME,
    )


def qr_code_data_uri(
    uri: str,
) -> str:

    image = qrcode.make(
        uri
    )

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG",
    )

    encoded = base64.b64encode(
        buffer.getvalue()
    ).decode(
        "ascii"
    )

    return (
        "data:image/png;base64,"
        + encoded
    )


def verify_totp(
    secret: str,
    code: str,
) -> bool:

    code = (
        code
        .replace(" ", "")
        .strip()
    )

    if not (
        code.isdigit()
        and len(code) == 6
    ):
        return False

    return pyotp.TOTP(
        secret
    ).verify(
        code,
        valid_window=1,
    )


def hash_recovery_code(
    code: str,
) -> str:

    normalized = (
        code
        .replace("-", "")
        .replace(" ", "")
        .upper()
    )

    return hashlib.sha256(
        normalized.encode(
            "utf-8"
        )
    ).hexdigest()


def generate_recovery_codes(
    db: Session,
    user: User,
    count: int = 10,
) -> list[str]:

    db.query(
        RecoveryCode
    ).filter(
        RecoveryCode.user_id
        == user.id
    ).delete()

    codes = []

    for _ in range(count):

        raw = secrets.token_hex(
            5
        ).upper()

        display = (
            raw[:5]
            + "-"
            + raw[5:]
        )

        codes.append(
            display
        )

        db.add(
            RecoveryCode(
                user_id=user.id,

                code_hash=
                    hash_recovery_code(
                        display
                    ),
            )
        )

    db.commit()

    return codes


def use_recovery_code(
    db: Session,
    user: User,
    code: str,
) -> bool:

    code_hash = (
        hash_recovery_code(
            code
        )
    )

    record = (
        db.query(
            RecoveryCode
        )
        .filter(
            RecoveryCode.user_id
            == user.id,

            RecoveryCode.code_hash
            == code_hash,

            RecoveryCode.used_at
            .is_(None),
        )
        .first()
    )

    if not record:
        return False

    record.used_at = (
        datetime.utcnow()
    )

    db.commit()

    return True