"""Authentication and encryption helpers for linked Craftarr nodes."""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .config import SECRET_KEY
from .models import NodeAccessToken


TOKEN_PREFIX = "craftarr_node_"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_node_token(db: Session) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(48)
    token_hash = _token_hash(token)
    record = db.get(NodeAccessToken, 1)
    if record is None:
        record = NodeAccessToken(id=1, token_hash=token_hash)
        db.add(record)
    else:
        record.token_hash = token_hash
    db.commit()
    return token


def verify_node_token(db: Session, token: str | None) -> bool:
    if not token or not token.startswith(TOKEN_PREFIX):
        return False
    record = db.get(NodeAccessToken, 1)
    if record is None:
        return False
    return hmac.compare_digest(record.token_hash, _token_hash(token))


def node_token_status(db: Session) -> dict:
    record = db.get(NodeAccessToken, 1)
    return {
        "active": record is not None,
        "created_at": record.created_at.isoformat() if record else None,
    }


def _cipher() -> Fernet:
    key_material = hmac.new(
        SECRET_KEY.encode("utf-8"),
        b"craftarr-remote-node-credential-v1",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def encrypt_remote_token(token: str) -> str:
    return _cipher().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_remote_token(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise ValueError(
            "The saved remote token cannot be decrypted. Check that this console's "
            "CRAFTARR_CONSOLE_SECRET has not changed."
        ) from error
