import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_ph = PasswordHasher()
# Hash factice : on vérifie quand même un mot de passe si l'email est inconnu, pour ne pas révéler
# par le temps de réponse qu'un compte existe.
_DUMMY_HASH = _ph.hash("mot-de-passe-factice-pour-egaliser-le-temps")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def new_session_token() -> tuple[str, str]:
    """(jeton brut pour le cookie, hash à stocker en base)"""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
