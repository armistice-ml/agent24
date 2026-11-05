from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
