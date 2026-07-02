"""Envelope-encrypted credential service (v1-definition Q9).

Credentials are stored in ``connector_credentials`` using a two-layer encryption
scheme implemented via PostgreSQL's ``pgcrypto`` extension (already enabled by
the Liquibase changeset ``0001-extensions``):

    plaintext secret
        └─► pgp_sym_encrypt(secret, data_key)          → ciphertext  (BYTEA)
    data_key (random, 64-hex chars)
        └─► pgp_sym_encrypt(data_key, CRED_MASTER_KEY) → enc_data_key (BYTEA)

Only ciphertext + enc_data_key are persisted. The plaintext secret and the
data_key never touch disk. The master key (``CRED_MASTER_KEY``) lives in a
Render secret; rotating it invalidates all stored credentials and must be done
with a simultaneous re-encryption migration.

Invariants:
* Write-only from the UI: this module never returns the plaintext secret.
* Admin-gated: callers enforce the role check before calling :func:`store`.
* Audit trail: every write emits a ``credential_audit`` row.
* Degrade gracefully: when ``CRED_MASTER_KEY`` is absent, operations raise
  :class:`CredentialServiceError` rather than silently storing unprotected data.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import settings

logger = logging.getLogger("grx10.services.credential")


class CredentialServiceError(RuntimeError):
    """Raised when a credential operation cannot be completed safely."""


def _require_master_key() -> str:
    """Return the master key or raise if unconfigured."""
    key = settings.CRED_MASTER_KEY
    if not key:
        raise CredentialServiceError(
            "CRED_MASTER_KEY is not configured. "
            "Set it as a Render secret before storing credentials."
        )
    return key


def _pgp_encrypt(session: Session, plaintext: str, passphrase: str) -> bytes:
    """Encrypt *plaintext* with *passphrase* via ``pgp_sym_encrypt`` and return raw bytes."""
    result: bytes = session.execute(
        text("SELECT pgp_sym_encrypt(:plaintext, :passphrase)"),
        {"plaintext": plaintext, "passphrase": passphrase},
    ).scalar_one()
    return bytes(result)


def _pgp_decrypt(session: Session, ciphertext: bytes, passphrase: str) -> str:
    """Decrypt *ciphertext* with *passphrase* via ``pgp_sym_decrypt`` and return text."""
    result: str = session.execute(
        text("SELECT pgp_sym_decrypt(:ciphertext, :passphrase)"),
        {"ciphertext": ciphertext, "passphrase": passphrase},
    ).scalar_one()
    return str(result)


def store(
    session: Session,
    *,
    source_id: str,
    secret: str,
    actor: str,
) -> str:
    """Encrypt and persist a connector secret.

    Generates a random per-credential data key, double-encrypts the secret, and
    upserts a ``connector_credentials`` row. Updates ``sources.auth_secret_ref``
    to the returned ``cred_ref`` and appends a ``credential_audit`` row.

    Parameters
    ----------
    session:
        A bound SQLAlchemy session (the caller holds the transaction).
    source_id:
        The ``sources.source_id`` this credential belongs to.
    secret:
        The plaintext API key / token (never stored, never returned).
    actor:
        Email or user-id of the admin performing the write (audit trail).

    Returns
    -------
    str
        The ``cred_ref`` that was written to ``sources.auth_secret_ref``.
    """
    master_key = _require_master_key()

    # Determine the new cred_ref and whether this is an add or rotation.
    existing_ref: str | None = session.execute(
        text("SELECT auth_secret_ref FROM sources WHERE source_id = :sid"),
        {"sid": source_id},
    ).scalar_one_or_none()

    cred_ref = existing_ref or f"cred_{source_id}"
    # Decide add-vs-rotate on whether the credential ROW exists — NOT on whether
    # sources.auth_secret_ref is set. The config seed pre-populates that pointer
    # for sources that have no credential row yet, so keying off it would route
    # the first real save into a no-op UPDATE.
    cred_row_exists = (
        session.execute(
            text("SELECT 1 FROM connector_credentials WHERE cred_ref = :ref"),
            {"ref": cred_ref},
        ).scalar_one_or_none()
        is not None
    )
    action = "rotated" if cred_row_exists else "added"

    # Generate a fresh random data key (64 hex chars = 256-bit entropy).
    data_key = secrets.token_hex(32)

    # Layer 1: encrypt the plaintext secret with the data key.
    ciphertext = _pgp_encrypt(session, secret, data_key)
    # Layer 2: wrap the data key with the master key.
    enc_data_key = _pgp_encrypt(session, data_key, master_key)

    now = datetime.now(timezone.utc)

    if cred_row_exists:
        # Rotation: update existing row with new ciphertext + wrapped key.
        session.execute(
            text("""
                UPDATE connector_credentials
                SET ciphertext = :ciphertext,
                    enc_data_key = :enc_data_key,
                    created_by = :actor,
                    rotated_at = :now
                WHERE cred_ref = :cred_ref
            """),
            {
                "ciphertext": ciphertext,
                "enc_data_key": enc_data_key,
                "actor": actor,
                "now": now,
                "cred_ref": cred_ref,
            },
        )
    else:
        # First-time insertion.
        session.execute(
            text("""
                INSERT INTO connector_credentials
                    (cred_ref, source_id, ciphertext, enc_data_key, created_by, created_at)
                VALUES
                    (:cred_ref, :source_id, :ciphertext, :enc_data_key, :actor, :now)
                ON CONFLICT (cred_ref) DO UPDATE
                    SET ciphertext    = EXCLUDED.ciphertext,
                        enc_data_key  = EXCLUDED.enc_data_key,
                        created_by    = EXCLUDED.created_by,
                        rotated_at    = EXCLUDED.created_at
            """),
            {
                "cred_ref": cred_ref,
                "source_id": source_id,
                "ciphertext": ciphertext,
                "enc_data_key": enc_data_key,
                "actor": actor,
                "now": now,
            },
        )
        # Wire the pointer in the sources row.
        session.execute(
            text("UPDATE sources SET auth_secret_ref = :ref WHERE source_id = :sid"),
            {"ref": cred_ref, "sid": source_id},
        )

    # Audit trail.
    session.execute(
        text("""
            INSERT INTO credential_audit (cred_ref, action, actor, at)
            VALUES (:cred_ref, :action, :actor, :now)
        """),
        {"cred_ref": cred_ref, "action": action, "actor": actor, "now": now},
    )

    logger.info("credential %s for source %r by %s", action, source_id, actor)
    return cred_ref


def retrieve(session: Session, *, cred_ref: str) -> str | None:
    """Decrypt and return the plaintext secret for *cred_ref*.

    Returns ``None`` when the ``cred_ref`` does not exist or the master key is
    not configured (safe degradation — callers treat ``None`` as no credential).
    Never raises; errors are logged.
    """
    try:
        master_key = _require_master_key()
    except CredentialServiceError:
        logger.warning("CRED_MASTER_KEY absent; cannot decrypt credential %r", cred_ref)
        return None

    row = session.execute(
        text(
            "SELECT ciphertext, enc_data_key "
            "FROM connector_credentials WHERE cred_ref = :ref"
        ),
        {"ref": cred_ref},
    ).one_or_none()

    if row is None:
        logger.warning("no credential found for cred_ref=%r", cred_ref)
        return None

    ciphertext, enc_data_key = bytes(row.ciphertext), bytes(row.enc_data_key)

    try:
        data_key = _pgp_decrypt(session, enc_data_key, master_key)
        plaintext = _pgp_decrypt(session, ciphertext, data_key)
        return plaintext
    except Exception as exc:  # noqa: BLE001 — decryption errors must not propagate
        logger.error("decryption failed for cred_ref=%r: %s", cred_ref, exc)
        return None


def revoke(session: Session, *, source_id: str, actor: str) -> bool:
    """Remove the credential for *source_id* and audit the removal.

    Returns ``True`` when a credential existed and was removed, ``False`` when
    there was nothing to revoke. Clears ``sources.auth_secret_ref``.
    """
    cred_ref: str | None = session.execute(
        text("SELECT auth_secret_ref FROM sources WHERE source_id = :sid"),
        {"sid": source_id},
    ).scalar_one_or_none()

    if not cred_ref:
        return False

    session.execute(
        text("DELETE FROM connector_credentials WHERE cred_ref = :ref"),
        {"ref": cred_ref},
    )
    session.execute(
        text("UPDATE sources SET auth_secret_ref = NULL WHERE source_id = :sid"),
        {"sid": source_id},
    )
    session.execute(
        text("""
            INSERT INTO credential_audit (cred_ref, action, actor, at)
            VALUES (:cred_ref, 'removed', :actor, now())
        """),
        {"cred_ref": cred_ref, "actor": actor},
    )
    logger.info("credential removed for source %r by %s", source_id, actor)
    return True


__all__ = [
    "CredentialServiceError",
    "store",
    "retrieve",
    "revoke",
]
