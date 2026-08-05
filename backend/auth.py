"""
auth.py — password security helpers
=====================================
Handles turning passwords into secure hashes (never storing plain text) and
checking a login attempt against a stored hash.

Why hashing: if the database were ever seen by someone who shouldn't, plain
passwords would be exposed. A hash is a one-way scramble — you can check a
match but can't reverse it back to the password.

We use bcrypt directly (the industry standard for password hashing).
"""

import bcrypt


def hash_password(plain_password: str) -> str:
    """Turn a plain password into a secure hash for storage.
    bcrypt works on bytes and caps input at 72 bytes, so we encode and trim."""
    pw_bytes = plain_password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check whether a login attempt matches the stored hash."""
    pw_bytes = plain_password.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pw_bytes, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
