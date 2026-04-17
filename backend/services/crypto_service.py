from cryptography.fernet import Fernet
from config import settings
import json

_key = settings.encryption_key.encode() if settings.encryption_key else Fernet.generate_key()
_fernet = Fernet(_key)


def encrypt(data: dict) -> str:
    return _fernet.encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str) -> dict:
    return json.loads(_fernet.decrypt(token.encode()).decode())
