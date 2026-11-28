"""Trusted operation receipts from one execution authority.

W04 and W05 are not interchangeable. A W04 absence is a missing row in a
consistent snapshot whose persisted repository and application binding match.
A W05 file that only repeats its own digest is not that proof. A missing
W05 transaction is unknown, not absence. An unrelated W05 read does not change a W04 result. When the snapshot
cannot be shown to be consistent, the status is ``unknown`` and must not be
used to retry the operation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from app.control_diagnostics.safe_io import UnsafePath, lstat_at, open_directory, open_root, read_bounded
from app.control_diagnostics.sqlite_fd import verified_sqlite_copy


def query_operation_receipt(
    root: Path,
    *,
    operation_id: str,
    expected_revision: int,
    candidate_digest: str,
    repo_id: str,
    app_id: str,
    authority: str,
) -> dict:
    if authority == "W04":
        return _w04(root, operation_id, expected_revision, candidate_digest, repo_id, app_id)
    if authority == "W05":
        return _w05(root, operation_id, expected_revision, candidate_digest, repo_id, app_id)
    return {"status": "unknown"}


def _w04(root: Path, operation_id: str, expected_revision: int, candidate_digest: str, repo_id: str, app_id: str) -> dict:
    schedule = Path(root) / "scheduler"
    database = schedule / "scheduler.sqlite3"
    try:
        if database.is_symlink() or not database.is_file():
            return {"status": "unknown"}
    except OSError:
        return {"status": "unknown"}
    try:
        with verified_sqlite_copy(schedule, "scheduler.sqlite3") as copied:
            connection = sqlite3.connect(Path(copied).as_uri() + "?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                checked = connection.execute("PRAGMA integrity_check").fetchone()
                if checked is None or checked[0] != "ok":
                    return {"status": "unknown"}
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "operation_receipts" not in tables or "repo_binding" not in tables or "tasks" not in tables:
                    return {"status": "unknown"}
                bound = {row["repo_id"] for row in connection.execute("SELECT repo_id FROM repo_binding")}
                if bound != {repo_id}:
                    return {"status": "unknown"}
                app_row = connection.execute("SELECT 1 FROM tasks WHERE app_id=? LIMIT 1", (app_id,)).fetchone()
                if app_row is None:
                    return {"status": "unknown"}
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    return {"status": "absent"}
                return _applied_row(row, operation_id, expected_revision, candidate_digest, repo_id, app_id)
            finally:
                connection.close()
    except (OSError, UnsafePath, sqlite3.Error):
        return {"status": "unknown"}


def _applied_row(row, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> dict:
    if row["status"] != "applied":
        return {"status": "unknown"}
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
    except (TypeError, ValueError):
        return {"status": "unknown"}
    if not isinstance(result, dict):
        return {"status": "unknown"}
    found = {
        "status": "applied",
        "operation_id": row["operation_id"],
        "expected_revision": int(row["expected_revision"]),
        "candidate_digest": row["candidate_digest"],
        "repo_id": row["repo_id"],
        "app_id": row["app_id"],
        "result": result,
    }
    if not _matches(found, operation_id, expected_revision, candidate_digest, repo_id, app_id):
        return {"status": "unknown"}
    return found


def _w05(root: Path, operation_id: str, expected_revision: int, candidate_digest: str, repo_id: str, app_id: str) -> dict:
    """Fail closed until a real W05 business receipt authority is connected."""

    # Earlier standalone journal records were not tied to an authorized
    # delivery transaction. They cannot be grandfathered in as proof.
    # Enable this port only once the actual business CAS and trusted W01
    # acknowledgement are committed together and independently verified.
    return {"status": "unknown", "reason": "BUSINESS_TRANSACTION_REQUIRED"}


def verify_isolated_business_commit(root: Path, *, events_root: Path, event_id: str,
                                    repo_id: str, app_id: str, task_id: str,
                                    request_digest: str, expected_revision: int) -> dict:
    """Verify a signed isolated intent AND its exact local business commit.

    Roots and scope must come from trusted server configuration. This is
    read-only and never authorizes retry or promotes an isolated operation
    to the production receipt API. Later object revisions are unknown,
    not proof that an old operation was absent.
    """
    import fcntl
    import hashlib
    import re
    from contextlib import ExitStack
    from app.control_delivery.ports import event_payload_digest
    from app.control_events.trusted_read import query_exact_event

    unknown = {"status": "unknown", "production_effective": False}
    key_pattern = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
    digest_pattern = re.compile(r"[a-f0-9]{64}")

    def safe_key(value):
        return isinstance(value, str) and key_pattern.fullmatch(value) is not None and value not in {".", ".."}

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

    if (not safe_key(event_id) or not isinstance(request_digest, str)
            or digest_pattern.fullmatch(request_digest) is None
            or type(expected_revision) is not int or expected_revision < 0
            or not all(isinstance(v, str) and v for v in (repo_id, app_id, task_id))):
        return unknown
    try:
        with ExitStack() as stack:
            def held(fd):
                stack.callback(os.close, fd)
                return fd

            root_fd = held(open_root(root))
            delivery_fd = held(open_directory(root_fd, "delivery"))
            lock_fd = held(os.open(".journal.lock", os.O_RDONLY | os.O_NOFOLLOW |
                                   os.O_NONBLOCK, dir_fd=delivery_fd))
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o022):
                return unknown
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            try:
                lstat_at(delivery_fd, ".journal.pending.json")
                return unknown
            except FileNotFoundError:
                pass

            def read(kind, key):
                if not safe_key(kind) or not safe_key(key):
                    raise ValueError("unsafe journal coordinate")
                folder = open_directory(delivery_fd, kind)
                try:
                    value = json.loads(read_bounded(folder, key + ".json", 262144).decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("not a journal object")
                    return value
                finally:
                    os.close(folder)

            commit = read("business-commit", event_id)
            outbox = read("outbox", event_id)
            event = outbox["event"]
            receipt = outbox["receipt"]
            scope = {"repo": repo_id, "task_id": task_id}
            mode = commit.get("intent_mode")
            if mode not in {"signed-isolated-v1", "signed-business-v1"}:
                return unknown
            if (commit.get("authority") != "W05_LOCAL" or commit.get("production_effective") is not False
                    or commit.get("intent_mode") != mode or outbox.get("intent_mode") != mode
                    or outbox.get("delivery_state") != "RECEIPT_RECORDED"
                    or outbox.get("local_is_authority") is not False
                    or commit.get("event_id") != event_id or outbox.get("event_id") != event_id
                    or commit.get("scope") != scope or outbox.get("scope") != scope
                    or commit.get("request_digest") != request_digest or outbox.get("request_digest") != request_digest
                    or type(commit.get("expected_revision")) is not int
                    or commit["expected_revision"] != expected_revision
                    or event.get("event_id") != event_id or event.get("repo_id") != repo_id
                    or event.get("app_id") != app_id or event.get("task_id") != task_id
                    or event.get("operation") != commit.get("operation")
                    or event.get("idempotency_key") != commit.get("idem_key")
                    or digest(event) != commit.get("event_digest")
                    or digest(receipt) != commit.get("receipt_digest")):
                return unknown
            if (receipt.get("isolated_signature_verified") is not True or receipt.get("w01_proof") is not False
                    or receipt.get("production_effective") is not False
                    or receipt.get("event_digest") != event_payload_digest(event)):
                return unknown
            changes = commit["changes"]
            if not isinstance(changes, list) or not 1 <= len(changes) <= 200:
                return unknown
            plan = []
            seen = set()
            for change in changes:
                kind, key = change["kind"], change["key"]
                if not safe_key(kind) or not safe_key(key) or (kind, key) in seen:
                    return unknown
                seen.add((kind, key))
                old, new = change["expected_revision"], change["revision"]
                if type(old) is not int or type(new) is not int or old < 0 or new != old + 1:
                    return unknown
                row = read(kind, key)
                fence = read("fence", kind + "__" + key)
                if (type(row.get("revision")) is not int or row["revision"] != new
                        or digest(row) != change["digest"] or fence.get("state") != "closed"
                        or fence.get("event_id") != event_id):
                    return unknown
                plan.append({"kind": kind, "key": key, "expected_revision": old})
            if outbox.get("change_plan") != plan:
                return unknown
            idem = read("idem", commit["idem_key"])
            if (idem.get("payload_hash") != request_digest or idem.get("state") != commit.get("state")
                    or idem.get("public") != commit.get("result")):
                return unknown
            intent_material = {
                "principal_id": commit["principal_id"], "scope": scope,
                "operation": commit["operation"], "request_digest": request_digest,
                "expected_revision": expected_revision, "changes": changes,
                "result": commit["result"],
            }
            business_verified = False
            if mode == "signed-business-v1":
                from app.control_authz import AuthzService, PrincipalRegistry
                from app.control_events.auth_bind import POLICY_REVISION
                from app.control_joint.w05_issue import verify_feedback_submit_proof

                auth = commit.get("business_auth")
                if (not isinstance(auth, dict) or auth != outbox.get("business_auth")
                        or auth != idem.get("business_auth") or idem.get("intent_mode") != mode
                        or auth.get("principal_id") != commit.get("principal_id")
                        or read("configuration", "business-mode") != {"mode": mode}
                        or commit.get("operation") != "feedback.submit"
                        or len(changes) != 1 or changes[0].get("kind") != "feedback"):
                    return unknown
                reservation = read("business-nonce", auth["nonce_key"])
                proof_record = read("business-proof", event_id)
                if (reservation != {"event_id": event_id, "authorization_digest": auth["authorization_digest"]}
                        or proof_record.get("event_id") != event_id
                        or proof_record.get("business_auth") != auth):
                    return unknown
                envelope = proof_record["signed"]
                feedback = read("feedback", changes[0]["key"])
                payload = {k: feedback[k] for k in ("author_id", "feedback_id", "category", "body")}
                if digest(payload) != request_digest:
                    return unknown
                if digest({"principal": auth["principal_id"], "repo": repo_id, "task_id": task_id,
                           "operation": "feedback.submit", "key": envelope["idempotency_key"]}) != commit["idem_key"]:
                    return unknown
                events_fd = held(open_root(events_root))
                state = json.loads(read_bounded(events_fd, "auth-state.json", 1048576).decode("utf-8"))
                if (not isinstance(state, dict) or not isinstance(state.get("principals"), dict)
                        or not isinstance(state.get("policy"), dict)
                        or not isinstance(state.get("revoked_signers"), list)):
                    return unknown
                authz = AuthzService(registry=PrincipalRegistry(isolated=True),
                                     isolated=True, policy_revision=POLICY_REVISION)
                if verify_feedback_submit_proof(
                        authz, state, repo_id=repo_id, signed=envelope,
                        scope={**scope, "app_id": app_id}, payload=payload,
                        idempotency_key=envelope["idempotency_key"],
                        expected_revision=expected_revision) != auth:
                    return unknown
                intent_material["business_auth"] = auth
                business_verified = True
            elif any(item.get("business_auth") is not None for item in (commit, outbox, idem)):
                return unknown
            intent = "w05-intent-v1:" + digest(intent_material)
            if intent != commit.get("intent_binding") or intent != event.get("note"):
                return unknown
            signed = query_exact_event(events_root, event_id=event_id, repo_id=repo_id,
                                       app_id=app_id, task_id=task_id)
            if not isinstance(signed, dict):
                return unknown
            proof = signed.get("read_receipt")
            if (not isinstance(proof, dict) or proof.get("evidence") != "verified"
                    or proof.get("authority") != "W01" or proof.get("server_source") != "app_service"
                    or proof != receipt.get("signed_intent_receipt")
                    or signed.get("event_type") != "delivery.intent.recorded"
                    or signed.get("idempotency_key") != event_payload_digest(event)
                    or signed.get("note") != intent or signed.get("evidence_ref") != intent
                    or any(signed.get(k) != event.get(k) for k in
                           ("stream_id", "stream_epoch", "producer_seq", "run_id"))):
                return unknown
            return {"status": "verified_isolated", "event_id": event_id,
                    "business_commit_verified": True, "business_signature_verified": business_verified,
                    "production_effective": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return unknown


def _delivery_json(root: Path, kind: str, key: str):
    """Read one journal object. Missing and unsafe paths are both unknown."""

    root_fd = None
    delivery_fd = None
    folder_fd = None
    try:
        root_fd = open_root(root)
        try:
            info = lstat_at(root_fd, "delivery")
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
        delivery_fd = open_directory(root_fd, "delivery")
        try:
            folder = lstat_at(delivery_fd, kind)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            return None
        folder_fd = open_directory(delivery_fd, kind)
        filename = key + ".json"
        try:
            file_info = lstat_at(folder_fd, filename)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
            return None
        document = json.loads(read_bounded(folder_fd, filename, 65536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, UnsafePath):
        return None
    finally:
        if folder_fd is not None:
            os.close(folder_fd)
        if delivery_fd is not None:
            os.close(delivery_fd)
        if root_fd is not None:
            os.close(root_fd)
    return document if isinstance(document, dict) else None


def _matches(item, operation_id, expected_revision, candidate_digest, repo_id, app_id) -> bool:
    return (
        item.get("operation_id") == operation_id
        and item.get("expected_revision") == expected_revision
        and item.get("candidate_digest") == candidate_digest
        and item.get("repo_id") == repo_id
        and item.get("app_id") == app_id
    )


def _safe_name(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80 and all(char.isalnum() or char in "._-" for char in value)
