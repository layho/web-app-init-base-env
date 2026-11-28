"""W01 read port for the joint board.

A stored payload digest is not a trusted source. A row is minted only after
the signature verifies over the W02 proof bytes rebuilt from this event.
``authority_json`` fields such as ``authorized`` and ``signed`` are not that
document. ``applied`` alone is not verification. Corruption that cannot be
attributed to the caller's repository and application is not counted in the
returned page.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

from app.control_authz.canonical import (
    PROOF_DOMAIN,
    PROOF_FIELDS,
    CanonicalError,
    idempotency_digest,
    parse_canonical,
    payload_hash,
    proof_message,
    scope_digest,
)
from app.control_authz.errors import AuthnError
from app.control_authz.principal import SOURCE_BY_KIND
from app.control_authz.proof import verify_signature
from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_root, read_bounded
from app.control_diagnostics.sqlite_fd import verified_sqlite_copy
from app.control_events.auth_bind import document_for_event
from app.control_events.engine import _identity
from app.control_events.errors import ControlError
from app.control_events.redact import redact
from app.control_events.schema import authorize_source

_PAGE = 50
_SCAN = 2000


class TrustedReadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def query_applied(
    root: Path,
    *,
    repo_id: str,
    app_id: str,
    task_ids: set[str],
    stream_id: str | None,
    limit: int = _PAGE,
    after_rowid: int = 0,
) -> dict:
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise TrustedReadError("page")
    if type(after_rowid) is not int or isinstance(after_rowid, bool) or after_rowid < 0:
        raise TrustedReadError("page")
    path = Path(root) / "events.sqlite3"
    if not path.exists():
        return _page([], truncated=False, next_rowid=None, bad=0, missing=True)
    connection = None
    try:
        registry = _registry(root)
        with verified_sqlite_copy(root, "events.sqlite3") as copied:
            connection = sqlite3.connect(copied)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA query_only=ON")
            return _collect(
                connection,
                registry=registry,
                repo_id=repo_id,
                app_id=app_id,
                task_ids=task_ids,
                stream_id=stream_id,
                limit=limit,
                after_rowid=after_rowid,
            )
    except TrustedReadError:
        raise
    except UnsafePath as exc:
        raise TrustedReadError(exc.kind if exc.kind else "unsafe") from exc
    except sqlite3.Error as exc:
        raise TrustedReadError("sqlite") from exc
    except OSError as exc:
        raise TrustedReadError("os") from exc
    finally:
        if connection is not None:
            connection.close()


def query_exact_event(root: Path, *, event_id: str, repo_id: str, app_id: str, task_id: str) -> dict | None:
    """Read one cryptographically verified applied event, never a fixture receipt.

    An absent, corrupt, mismatched or non-authoritative row is unknown.
    The caller supplies the trusted configured root and authorized scope.
    """
    if not all(isinstance(value, str) and 0 < len(value) <= 200
               for value in (event_id, repo_id, app_id, task_id)):
        return None
    try:
        registry = _registry(root)
        with verified_sqlite_copy(root, "events.sqlite3") as copied:
            connection = sqlite3.connect(Path(copied).as_uri() + "?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                row = connection.execute(
                    "SELECT * FROM events WHERE event_id=? AND applied=1", (event_id,)
                ).fetchone()
                if row is None:
                    return None
                payload = _payload(row)
                if not isinstance(payload, dict):
                    return None
                expected = {"event_id": event_id, "repo_id": repo_id, "app_id": app_id, "task_id": task_id}
                if any(payload.get(key) != value for key, value in expected.items()):
                    return None
                if any(payload.get(key) != row[key] for key in ("event_id", "stream_id", "stream_epoch", "producer_seq")):
                    return None
                event = _mint(connection, row, payload, registry)
                if event is None or event["read_receipt"]["evidence"] != "verified":
                    return None
                return event
            finally:
                connection.close()
    except (OSError, UnsafePath, sqlite3.Error, TrustedReadError, ValueError, TypeError, KeyError, IndexError):
        return None


def _registry(root: Path) -> dict:
    root_fd = open_root(root)
    try:
        try:
            info = lstat_at(root_fd, "auth-state.json")
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TrustedReadError("registry")
        document = json.loads(read_bounded(root_fd, "auth-state.json", 1_000_000).decode("utf-8"))
    except (UnicodeError, ValueError, UnsafePath) as exc:
        raise TrustedReadError("registry") from exc
    finally:
        import os
        os.close(root_fd)
    if not isinstance(document, dict) or not isinstance(document.get("principals"), dict):
        raise TrustedReadError("registry")
    return document["principals"]


def _collect(connection, *, registry, repo_id, app_id, task_ids, stream_id, limit, after_rowid):
    items = []
    bad = 0
    cursor = after_rowid
    scanned = 0
    truncated = False
    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    if "authority_json" not in columns:
        raise TrustedReadError("authority")
    while len(items) < limit and scanned < _SCAN:
        rows = connection.execute(
            """
            SELECT rowid, payload_hash, payload_json, received_at, applied, authority_json
            FROM events
            WHERE applied=1 AND rowid>?
            ORDER BY rowid
            """,
            (cursor,),
        ).fetchmany(limit)
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            cursor = int(row["rowid"])
            payload = _payload(row)
            minted = _mint(connection, row, payload, registry)
            if minted is None:
                if _owned(payload, repo_id, app_id):
                    bad += 1
                continue
            if minted.get("repo_id") != repo_id or minted.get("app_id") != app_id:
                continue
            task_id = minted.get("task_id") or minted.get("subject_id")
            if task_id not in task_ids:
                continue
            if stream_id is not None and minted.get("stream_id") != stream_id:
                continue
            items.append(minted)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            extra = connection.execute(
                "SELECT 1 FROM events WHERE applied=1 AND rowid>? LIMIT 1",
                (cursor,),
            ).fetchone()
            truncated = extra is not None
            break
        if len(rows) < limit:
            break
    else:
        if scanned >= _SCAN:
            extra = connection.execute(
                "SELECT 1 FROM events WHERE applied=1 AND rowid>? LIMIT 1",
                (cursor,),
            ).fetchone()
            truncated = extra is not None
    next_rowid = cursor if truncated else None
    return _page(items, truncated=truncated, next_rowid=next_rowid, bad=bad, missing=False)


def _payload(row):
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _owned(payload, repo_id, app_id) -> bool:
    return isinstance(payload, dict) and payload.get("repo_id") == repo_id and payload.get("app_id") == app_id


def _mint(connection, row, payload, registry):
    if payload is None:
        return None
    authority = row["payload_hash"]
    if not isinstance(authority, str) or not authority:
        return None
    checked = {key: value for key, value in payload.items() if key != "received_at"}
    if _identity(checked) != authority:
        return None
    verdict = _verdict(connection, row, payload, registry, authority)
    if verdict is None:
        return None
    server_source, authoritative = verdict
    event = redact(dict(payload))
    event.pop("read_receipt", None)
    event.pop("submit_receipt", None)
    event["payload_digest"] = authority
    received = row["received_at"] if isinstance(row["received_at"], str) and row["received_at"] else None
    if received is None:
        event.pop("received_at", None)
    else:
        event["received_at"] = received
    verified = bool(authoritative and row["applied"])
    event["read_receipt"] = {
        "kind": "read",
        "authority": "W01",
        "authority_digest": authority,
        "event_id": event.get("event_id"),
        "payload_digest": authority,
        "stream_id": event.get("stream_id"),
        "stream_epoch": event.get("stream_epoch"),
        "producer_seq": event.get("producer_seq"),
        "server_source": server_source,
        "fact_state": "ACCEPTED" if verified else None,
        "evidence": "verified" if verified else "recorded",
        "received_at": received,
    }
    if event["read_receipt"]["authority_digest"] != authority:
        return None
    if event["read_receipt"]["server_source"] != server_source:
        return None
    return event


def _verdict(connection, row, payload, registry, digest):
    """Verify the signature over the proof rebuilt from this event.

    ``authorized`` and the stored ``signed`` object are writable columns.
    They are not the W02 plaintext and they are not the historical decision.
    """

    try:
        authority = json.loads(row["authority_json"]) if isinstance(row["authority_json"], str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(authority, dict):
        return None
    signature = authority.get("signature")
    message_hex = authority.get("message_hex")
    if not isinstance(signature, str) or not isinstance(message_hex, str):
        return None
    try:
        raw = bytes.fromhex(message_hex)
    except ValueError:
        return None
    signed = _proof_plaintext(raw)
    if signed is None or signed["operation"] != "event.append" or signed["alg"] != "ed25519":
        return None
    principal_id = signed["signer_id"]
    record = registry.get(principal_id) if isinstance(principal_id, str) and principal_id else None
    if not isinstance(record, dict):
        return None
    server_source = SOURCE_BY_KIND.get(record.get("kind"))
    if not isinstance(server_source, str) or not server_source or server_source == "authoritative":
        return None
    if payload.get("event_type") == "delivery.intent.recorded":
        if (record.get("kind") != "app_service" or record.get("producer_id") != "w05"
                or record.get("repo_id") != payload.get("repo_id")
                or record.get("app_id") != payload.get("app_id")
                or record.get("stream_id") != payload.get("stream_id")
                or "delivery.intent" not in (record.get("scopes") or [])
                or "task:" + str(payload.get("task_id")) not in (record.get("scopes") or [])):
            return None
    public = record.get("public_key")
    if signed["key_id"] != record.get("key_id") or signed["declared_id"] != principal_id or not isinstance(public, str):
        return None
    try:
        document = document_for_event(
            payload,
            operation="event.append",
            authorizer=principal_id,
            path="/api/v1/control/events",
        )
        if signed["payload_hash"] != payload_hash("event.append", document):
            return None
        if signed["scope"] != scope_digest(document["scope"]):
            return None
    except CanonicalError:
        return None
    for name in ("repo_id", "object_id", "candidate_id", "head", "base", "merge", "tree"):
        if signed[name] != document[name]:
            return None
    if signed["policy_revision"] != str(document["policy_revision"]):
        return None
    if not _nonce_binds(connection, record, principal_id, signed["nonce"], digest):
        return None
    try:
        verify_signature(public, signature, raw)
        decided = authorize_source(dict(payload), server_source)
    except (AuthnError, ControlError, ValueError):
        return None
    return server_source, bool(decided)


def _proof_plaintext(raw: bytes):
    """Rebuild the W02 proof message and require the stored bytes to match."""

    try:
        parsed = parse_canonical(raw)
    except CanonicalError:
        return None
    if len(parsed) < 2 or parsed[0] != ("version", "v1") or parsed[1] != ("domain", PROOF_DOMAIN):
        return None
    names = [name for name, _value in parsed[2:]]
    if names != list(PROOF_FIELDS):
        return None
    fields = {name: value for name, value in parsed[2:]}
    try:
        rebuilt = proof_message(fields)
    except CanonicalError:
        return None
    if rebuilt != raw:
        return None
    return fields


def _nonce_binds(connection, record, principal_id: str, nonce: str, digest: str) -> bool:
    """The nonce inside the signature must be the one consumed for this digest."""

    if not isinstance(nonce, str) or not nonce:
        return False
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False
    if "consumed_nonces" not in tables:
        return False
    slot = idempotency_digest(
        principal_id=principal_id,
        kind="nonce",
        repo_id=record.get("repo_id") if isinstance(record.get("repo_id"), str) else "",
        app_id=record.get("app_id") if isinstance(record.get("app_id"), str) else "",
        producer_id=record.get("producer_id") if isinstance(record.get("producer_id"), str) else "",
        stream_id=record.get("stream_id") if isinstance(record.get("stream_id"), str) else "",
        task_id="",
        credential_scope="",
        phase="",
        object_id="",
        operation="nonce",
        idempotency_key=nonce,
        target_app_id="",
    )
    row = connection.execute(
        "SELECT payload_hash, operation, principal_id FROM consumed_nonces WHERE slot=?",
        (slot,),
    ).fetchone()
    if row is None:
        return False
    return row["payload_hash"] == digest and row["operation"] == "event.append" and row["principal_id"] == principal_id


def _page(items, *, truncated, next_rowid, bad, missing):
    return {
        "items": items,
        "truncated": truncated,
        "next_rowid": next_rowid,
        "bad": bad,
        "missing": missing,
        "blocked": False,
    }
