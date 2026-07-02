"""Connector-credential service — envelope encryption at rest (Q9).

Every connector secret (API key, login password, OAuth token) is stored in
``connector_credentials`` as **ciphertext only**. Plaintext is never persisted and
never returned to the browser: the store is *write-only from the API*. The only
reader is the pipeline, server-side, decrypting in memory for the duration of a
single ``pull``.

Envelope scheme (pgcrypto, ``pgp_sym_*``)
-----------------------------------------
1. A fresh, high-entropy **data key** (DEK) is minted per credential
   (:func:`secrets.token_urlsafe`).
2. The secret is encrypted under the DEK::

       ciphertext = pgp_sym_encrypt(secret, data_key)

3. The DEK is itself wrapped under the Render master key
   (``settings.CRED_MASTER_KEY``)::

       enc_data_key = pgp_sym_encrypt(data_key, CRED_MASTER_KEY)

Both ciphertext columns are ``BYTEA``; the plaintext secret and the DEK travel
only as *bound parameters* into a single Postgres statement and are discarded the
moment it returns — neither is ever written to a column. Rotating a secret mints a
**new** DEK (the master key is unchanged), so a leaked DEK compromises one
credential, not the store.

Decryption reverses the envelope in one statement::

    pgp_sym_decrypt(ciphertext, pgp_sym_decrypt(enc_data_key, CRED_MASTER_KEY))

Why server-side crypto? pgcrypto keeps the encryption in the database engine, so
the application process never holds the master key longer than a parameter bind
and we get the same primitive (``pgp_sym_encrypt``) the DDL comment promises.

Master-key policy
-----------------
* **Writes** (:func:`store_credential`, :func:`rotate`) require the master key;
  without it they raise :class:`MasterKeyUnavailable` — we never write a secret we
  cannot encrypt, and never fall back to plaintext.
* **Reads** (:func:`get_plaintext`) *degrade gracefully*: a missing master key or
  missing row yields ``None`` (with a warning) so the connector simply has no
  credential — its ``probe()`` then returns ``AUTH_FAILED`` rather than crashing
  the pipeline. No data is fabricated.

Audit
-----
Every mutating operation appends a :class:`CredentialAudit` row
(``added`` | ``rotated`` | ``removed``) with the acting admin. Reads are not
audited (they happen on every pull and would drown the trail).

Public API: :func:`store_credential`, :func:`get_plaintext` (server-only),
:func:`rotate`, :func:`remove`, :func:`list_refs` (masked metadata).
"""

from __future__ import annotations

import datetime
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import ConnectorCredential, CredentialAudit

logger = logging.getLogger("grx10.credentials")

# Number of random bytes behind a per-credential data key. 48 bytes -> a 64-char
# URL-safe passphrase fed to pgp_sym_encrypt (far beyond brute-force reach).
_DATA_KEY_BYTES = 48

# The .env.example placeholder. Treated as "configured" (it is a usable passphrase
# for local dev) but flagged loudly so it never silently reaches production.
_PLACEHOLDER_MASTER_KEY = "change-me-base64-32-bytes"

# Constant shown by list_refs in place of any secret material.
_MASK = "••••••••"  # ••••••••


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CredentialError(RuntimeError):
    """Base class for credential-service failures."""


class MasterKeyUnavailable(CredentialError):
    """Raised by write paths when ``CRED_MASTER_KEY`` is not configured.

    Reads do not raise this; they return ``None`` and degrade.
    """


class CredentialNotFound(CredentialError):
    """Raised when an operation targets a ``cred_ref`` that does not exist."""


class CredentialExists(CredentialError):
    """Raised when :func:`store_credential` would overwrite an existing ref.

    Use :func:`rotate` to replace an existing secret.
    """


# --------------------------------------------------------------------------- #
# Masked view returned to the API / UI
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CredentialRef:
    """Non-secret metadata for one stored credential (safe to send to a browser).

    Carries no secret material whatsoever — ``masked`` is a fixed placeholder, not a
    truncation of the plaintext — so the listing endpoint cannot leak key bytes.
    """

    cred_ref: str
    source_id: str | None
    created_by: str | None
    created_at: datetime.datetime | None
    rotated_at: datetime.datetime | None
    masked: str = _MASK


# --------------------------------------------------------------------------- #
# Master-key handling
# --------------------------------------------------------------------------- #
def master_key_configured() -> bool:
    """True when a non-empty ``CRED_MASTER_KEY`` is present (callers can pre-check)."""
    return bool(settings.CRED_MASTER_KEY and settings.CRED_MASTER_KEY.strip())


def _require_master_key() -> str:
    """Return the master key for a write path, or raise :class:`MasterKeyUnavailable`."""
    key = settings.CRED_MASTER_KEY
    if not key or not key.strip():
        raise MasterKeyUnavailable(
            "CRED_MASTER_KEY is not set; refusing to store a credential we cannot "
            "encrypt. Set it in the environment (openssl rand -base64 32)."
        )
    if key.strip() == _PLACEHOLDER_MASTER_KEY:
        logger.warning(
            "CRED_MASTER_KEY is the .env.example placeholder — fine for local dev, "
            "MUST be replaced with a real secret before production."
        )
    return key


def _mint_data_key() -> str:
    """Mint a fresh, high-entropy per-credential data key (URL-safe text)."""
    return secrets.token_urlsafe(_DATA_KEY_BYTES)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def _audit(session: Session, *, cred_ref: str, action: str, actor: str | None) -> None:
    """Append a ``credential_audit`` row (``added`` | ``rotated`` | ``removed``)."""
    session.add(CredentialAudit(cred_ref=cred_ref, action=action, actor=actor))


# --------------------------------------------------------------------------- #
# Write paths (admin-gated at the router; Q9/Q10)
# --------------------------------------------------------------------------- #
def store_credential(
    session: Session,
    *,
    cred_ref: str,
    secret: str,
    source_id: str | None = None,
    actor: str | None = None,
) -> CredentialRef:
    """Encrypt and store a brand-new credential under ``cred_ref``.

    Mints a per-credential data key, encrypts ``secret`` under it, wraps the data
    key under the master key, and writes both ciphertexts in a single statement so
    no plaintext is ever persisted. Appends an ``added`` audit row.

    Args:
        session: Active SQLAlchemy session (the caller owns the transaction/commit).
        cred_ref: Stable pointer that ``sources.auth_secret_ref`` references. Must
            not already exist — use :func:`rotate` to replace a secret.
        secret: The plaintext credential (API key / password / token).
        source_id: Optional owning source id (FK into ``sources``).
        actor: The acting admin (e.g. their email), recorded in the audit trail.

    Returns:
        Masked :class:`CredentialRef` metadata (never the plaintext).

    Raises:
        ValueError: If ``cred_ref`` or ``secret`` is empty.
        MasterKeyUnavailable: If ``CRED_MASTER_KEY`` is not configured.
        CredentialExists: If ``cred_ref`` already has a stored secret.
    """
    if not cred_ref or not cred_ref.strip():
        raise ValueError("cred_ref must be a non-empty string.")
    if not secret:
        raise ValueError("secret must be a non-empty string.")

    master_key = _require_master_key()

    if session.get(ConnectorCredential, cred_ref) is not None:
        raise CredentialExists(
            f"Credential '{cred_ref}' already exists; call rotate() to replace it."
        )

    data_key = _mint_data_key()
    session.execute(
        text(
            """
            INSERT INTO connector_credentials
                (cred_ref, source_id, ciphertext, enc_data_key, created_by, created_at)
            VALUES
                (:cred_ref,
                 :source_id,
                 pgp_sym_encrypt(:secret, :data_key),
                 pgp_sym_encrypt(:data_key, :master_key),
                 :actor,
                 now())
            """
        ),
        {
            "cred_ref": cred_ref,
            "source_id": source_id,
            "secret": secret,
            "data_key": data_key,
            "master_key": master_key,
            "actor": actor,
        },
    )
    _audit(session, cred_ref=cred_ref, action="added", actor=actor)
    session.flush()
    logger.info("stored credential '%s' (source_id=%s) by %s", cred_ref, source_id, actor)
    return _ref_of(session, cred_ref)


def rotate(
    session: Session,
    *,
    cred_ref: str,
    secret: str,
    actor: str | None = None,
) -> CredentialRef:
    """Replace the secret behind ``cred_ref`` with a freshly-enveloped value.

    Mints a **new** data key (the master key is untouched), re-encrypts, updates the
    ciphertext columns, stamps ``rotated_at = now()`` and appends a ``rotated``
    audit row. The ``cred_ref`` and any ``sources.auth_secret_ref`` pointer remain
    valid, so rotation is transparent to connectors.

    Args:
        session: Active SQLAlchemy session.
        cred_ref: The existing credential to rotate.
        secret: The new plaintext credential.
        actor: The acting admin, recorded in the audit trail.

    Returns:
        Masked :class:`CredentialRef` metadata.

    Raises:
        ValueError: If ``secret`` is empty.
        MasterKeyUnavailable: If ``CRED_MASTER_KEY`` is not configured.
        CredentialNotFound: If ``cred_ref`` does not exist.
    """
    if not secret:
        raise ValueError("secret must be a non-empty string.")

    master_key = _require_master_key()

    if session.get(ConnectorCredential, cred_ref) is None:
        raise CredentialNotFound(f"Credential '{cred_ref}' does not exist.")

    data_key = _mint_data_key()
    session.execute(
        text(
            """
            UPDATE connector_credentials
               SET ciphertext   = pgp_sym_encrypt(:secret, :data_key),
                   enc_data_key = pgp_sym_encrypt(:data_key, :master_key),
                   rotated_at   = now()
             WHERE cred_ref = :cred_ref
            """
        ),
        {
            "cred_ref": cred_ref,
            "secret": secret,
            "data_key": data_key,
            "master_key": master_key,
        },
    )
    _audit(session, cred_ref=cred_ref, action="rotated", actor=actor)
    session.flush()
    logger.info("rotated credential '%s' by %s", cred_ref, actor)
    return _ref_of(session, cred_ref)


def remove(session: Session, *, cred_ref: str, actor: str | None = None) -> bool:
    """Delete the stored credential for ``cred_ref`` and audit the removal.

    The ``sources.auth_secret_ref`` pointer (a plain text column, not a FK) is left
    intact: deleting the secret is enough to disable the connector, and keeping the
    pointer lets an admin re-add a credential under the same ref later.

    Args:
        session: Active SQLAlchemy session.
        cred_ref: The credential to remove.
        actor: The acting admin, recorded in the audit trail.

    Returns:
        ``True`` if a row was deleted, ``False`` if nothing matched.
    """
    row = session.get(ConnectorCredential, cred_ref)
    if row is None:
        logger.info("remove() no-op: credential '%s' not found", cred_ref)
        return False
    session.delete(row)
    _audit(session, cred_ref=cred_ref, action="removed", actor=actor)
    session.flush()
    logger.info("removed credential '%s' by %s", cred_ref, actor)
    return True


# --------------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------------- #
def get_plaintext(session: Session, cred_ref: str) -> str | None:
    """Decrypt and return the plaintext secret for ``cred_ref`` — **server-only**.

    This is the single reader of secret material and is intended exclusively for the
    pipeline at pull time; the value is held in memory only for the call's duration
    and must never be serialised to the browser or logged.

    Degrades gracefully (returns ``None`` with a warning, never raises) when:

    * ``CRED_MASTER_KEY`` is not configured,
    * the ``cred_ref`` has no stored credential, or
    * decryption fails (wrong/rotated master key, corrupt ciphertext).

    A ``None`` result means the connector has no usable credential, so its
    ``probe()`` returns ``AUTH_FAILED`` and ``pull()`` yields nothing — we degrade,
    we do not fabricate.

    Args:
        session: Active SQLAlchemy session.
        cred_ref: The credential to decrypt.

    Returns:
        The plaintext secret, or ``None`` if unavailable.
    """
    if not master_key_configured():
        logger.warning(
            "get_plaintext('%s'): CRED_MASTER_KEY unset; cannot decrypt — "
            "connector will see no credential.",
            cred_ref,
        )
        return None

    try:
        result = session.execute(
            text(
                """
                SELECT pgp_sym_decrypt(
                           ciphertext,
                           pgp_sym_decrypt(enc_data_key, :master_key)
                       ) AS plaintext
                  FROM connector_credentials
                 WHERE cred_ref = :cred_ref
                """
            ),
            {"cred_ref": cred_ref, "master_key": settings.CRED_MASTER_KEY},
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — decode failures must degrade, not crash the pull
        logger.warning("get_plaintext('%s') failed to decrypt: %s", cred_ref, exc)
        return None

    if result is None:
        logger.warning("get_plaintext('%s'): no stored credential", cred_ref)
        return None
    return result


def list_refs(session: Session, *, source_id: str | None = None) -> list[CredentialRef]:
    """List stored credentials as **masked** metadata (no secret material).

    Safe to surface in the admin UI: returns only ``cred_ref``/``source_id``/owner/
    timestamps plus a fixed mask. Decryption is never performed here.

    Args:
        session: Active SQLAlchemy session.
        source_id: Optional filter to one source's credentials.

    Returns:
        Masked :class:`CredentialRef` rows, ordered by ``cred_ref``.
    """
    query = session.query(ConnectorCredential)
    if source_id is not None:
        query = query.filter(ConnectorCredential.source_id == source_id)
    rows = query.order_by(ConnectorCredential.cred_ref).all()
    return [_to_ref(row) for row in rows]


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _to_ref(row: ConnectorCredential) -> CredentialRef:
    """Project a :class:`ConnectorCredential` row to masked metadata."""
    return CredentialRef(
        cred_ref=row.cred_ref,
        source_id=row.source_id,
        created_by=row.created_by,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
    )


def _ref_of(session: Session, cred_ref: str) -> CredentialRef:
    """Re-read a credential row (post-write) and return its masked metadata."""
    row = session.get(ConnectorCredential, cred_ref)
    if row is None:  # pragma: no cover — we just wrote it
        raise CredentialNotFound(f"Credential '{cred_ref}' vanished after write.")
    return _to_ref(row)


__all__ = [
    "CredentialRef",
    "CredentialError",
    "MasterKeyUnavailable",
    "CredentialNotFound",
    "CredentialExists",
    "master_key_configured",
    "store_credential",
    "get_plaintext",
    "rotate",
    "remove",
    "list_refs",
]
