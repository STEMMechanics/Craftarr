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
    token_ciphertext = encrypt_node_token(token)
    record = db.get(NodeAccessToken, 1)
    if record is None:
        record = NodeAccessToken(
            id=1,
            token_hash=token_hash,
            token_ciphertext=token_ciphertext,
        )
        db.add(record)
    else:
        record.token_hash = token_hash
        record.token_ciphertext = token_ciphertext
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
    if record is None:
        return {
            "active": False,
            "token": None,
            "token_available": False,
            "token_issue": None,
            "created_at": None,
        }

    token = None
    token_issue = None
    if record.token_ciphertext:
        try:
            token = decrypt_node_token(record.token_ciphertext)
        except ValueError:
            token_issue = "decrypt_failed"
    else:
        token_issue = "legacy"

    return {
        "active": True,
        "token": token,
        "token_available": token is not None,
        "token_issue": token_issue,
        "created_at": record.created_at.isoformat(),
    }


def _cipher() -> Fernet:
    key_material = hmac.new(
        SECRET_KEY.encode("utf-8"),
        b"craftarr-remote-node-credential-v1",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def _encrypt_secret(secret: str) -> str:
    return _cipher().encrypt(secret.encode("utf-8")).decode("ascii")


def _decrypt_secret(ciphertext: str, label: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise ValueError(
            f"The saved {label} cannot be decrypted. Check that this console's "
            "CRAFTARR_CONSOLE_SECRET has not changed."
        ) from error


def encrypt_node_token(token: str) -> str:
    return _encrypt_secret(token)


def decrypt_node_token(ciphertext: str) -> str:
    return _decrypt_secret(ciphertext, "node access token")


def encrypt_remote_token(token: str) -> str:
    return _encrypt_secret(token)


def decrypt_remote_token(ciphertext: str) -> str:
    return _decrypt_secret(ciphertext, "remote token")
