"""
encryption.py - encrypt a short secret before it is stored in the database.

Used for government ID numbers (Aadhaar / PAN). The number is encrypted here, inside our
server, so what reaches Supabase is unreadable text: nobody looking at the table in the
Supabase dashboard, or at a database backup, can read an Aadhaar number.

The key lives in GOVERNMENT_ID_ENCRYPTION_KEY in the .env file, next to the other secrets.
Make one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Keep that key safe. If it is lost, the stored numbers cannot be read again and every employee
has to submit their ID once more (the super admin clears theirs, see hr/government_id).
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_required_setting

GOVERNMENT_ID_KEY_NAME = "GOVERNMENT_ID_ENCRYPTION_KEY"


class EncryptionError(Exception):
    """The stored text could not be read back (wrong key, or damaged text)."""


@lru_cache(maxsize=1)
def get_government_id_cipher():
    """The Fernet cipher built from the key in .env. Built once and reused."""
    key_text = get_required_setting(GOVERNMENT_ID_KEY_NAME)
    try:
        return Fernet(key_text.encode("utf-8"))
    except (ValueError, TypeError):
        raise RuntimeError(
            f"{GOVERNMENT_ID_KEY_NAME} is not a valid Fernet key. Make one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def encrypt_text(plain_text):
    """Turn a short secret into text that is safe to store."""
    return get_government_id_cipher().encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_text(encrypted_text):
    """Read back something encrypt_text() stored. Raises EncryptionError when the key does not match."""
    try:
        return get_government_id_cipher().decrypt(encrypted_text.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        raise EncryptionError("This stored value could not be read with the current encryption key.")
