from cryptography.fernet import Fernet
from config import config

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.ENCRYPTION_KEY
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(data: str) -> str:
    return _get_fernet().encrypt(data.encode()).decode()


def decrypt(data: str) -> str:
    return _get_fernet().decrypt(data.encode()).decode()
