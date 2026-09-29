from __future__ import annotations

import base64
import hashlib
import secrets


class ScryptSecretHasher:
    """Versioned scrypt encoding for break-glass credentials.

    The encoded value contains only algorithm parameters, a random salt and the
    derived key. The plaintext secret is never persisted or returned.
    """

    algorithm = "scrypt"
    n = 1 << 14
    r = 8
    p = 1
    dklen = 32
    salt_bytes = 16
    maxmem = 64 * 1024 * 1024

    def _derive(self, secret: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            str(secret).encode("utf-8"),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.dklen,
            maxmem=self.maxmem,
        )

    def hash_secret(self, secret: str) -> str:
        salt = secrets.token_bytes(self.salt_bytes)
        derived = self._derive(str(secret), salt)
        return "$".join(
            (
                self.algorithm,
                str(self.n),
                str(self.r),
                str(self.p),
                base64.urlsafe_b64encode(salt).decode("ascii"),
                base64.urlsafe_b64encode(derived).decode("ascii"),
            )
        )

    def verify_secret(self, secret: str, encoded_hash: str) -> bool:
        try:
            algorithm, n_text, r_text, p_text, salt_text, digest_text = str(
                encoded_hash
            ).split("$", 5)
            if algorithm != self.algorithm:
                return False
            if (
                int(n_text) != self.n
                or int(r_text) != self.r
                or int(p_text) != self.p
            ):
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            if len(salt) != self.salt_bytes or len(expected) != self.dklen:
                return False
            actual = self._derive(str(secret), salt)
        except (ValueError, TypeError, UnicodeError):
            return False
        return secrets.compare_digest(actual, expected)
