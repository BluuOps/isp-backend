import base64
import binascii
import hashlib
import hmac
import secrets


def generate_temporary_password() -> str:
    return secrets.token_urlsafe(15)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        310_000,
    )
    return "pbkdf2_sha256$310000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(iterations)
        if rounds < 100_000:
            return False
        salt = base64.urlsafe_b64decode(salt_value)
        expected = base64.urlsafe_b64decode(digest_value)
    except (TypeError, ValueError, binascii.Error):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        rounds,
    )
    return hmac.compare_digest(actual, expected)
