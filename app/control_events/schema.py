"""Schema 2 checks for new events. Old protocol records do not enter this path."""

from .contract_view import event_spec
from .errors import ControlError

ENVELOPE = ("actor", "scope", "expected_revision", "idempotency_key")
OPTIONAL = (
    "trusted_source",
    "classification",
    "epoch_closed",
    "closed_through_seq",
    "guard",
    "progress_status",
    "note",
    "observed_object_class",
    "observed_object_id",
    "delivery_channels",
    "candidate_id",
    "revision",
    "rollback",
    "app_id",
    "host_id",
    "successor_producer",
)


STRUCTURAL_TYPES = {
    "run.started": "task",
    "authorization.epoch_closed": "authorization",
    # Isolated W11 bridge; evidence only, never a run-state transition.
    "delivery.intent.recorded": "evidence",
}
_SHA = set("0123456789abcdef")


def _schema(message: str) -> ControlError:
    return ControlError("SCHEMA_UNSUPPORTED", 422, message)


def _text(value, field: str) -> None:
    if type(value) is not str or not value:
        raise _schema(f"{field} must be a string")


def _integer(value, field: str, *, minimum: int | None = None) -> None:
    if type(value) is not int:
        raise _schema(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise _schema(f"{field} is out of range")


def _sha(value, field: str) -> None:
    if type(value) is not str or len(value) != 40 or any(char not in _SHA for char in value):
        raise _schema(f"{field} must be a sha")


def split_request(body: dict) -> dict:
    if not isinstance(body, dict):
        raise _schema("request must be an object")
    if body.get("schema_version") != 2:
        raise _schema("schema_version must be 2")
    if "received_at" in body:
        raise _schema("received_at is filled by the server")
    for field in ENVELOPE:
        if body.get(field) in (None, ""):
            raise _schema(f"missing {field}")
    if not isinstance(body["actor"], dict) or not isinstance(body["scope"], dict):
        raise _schema("actor and scope must be objects")
    if type(body["expected_revision"]) is not int:
        raise _schema("expected_revision must be an integer")
    if type(body["idempotency_key"]) is not str:
        raise _schema("idempotency_key must be a string")
    event = _extract(body)
    validate_event(event)
    event["expected_revision"] = body["expected_revision"]
    return event


def _extract(body: dict) -> dict:
    spec = event_spec()
    event_class = body.get("event_class")
    if event_class not in spec["classes"]:
        raise _schema("event_class is not in the contract")
    fields = list(spec["always_required"]) + list(spec["classes"][event_class])
    event = {field: body.get(field) for field in fields}
    for field in OPTIONAL:
        if field in body:
            event[field] = body[field]
    if "task_id" in body and "task_id" not in event:
        event["task_id"] = body["task_id"]
    return event


def validate_event(event: dict) -> None:
    spec = event_spec()
    if event.get("schema_version") != 2:
        raise _schema("schema_version must be 2")
    if "received_at" in event:
        raise _schema("received_at is filled by the server")
    for field in spec["always_required"]:
        if event.get(field) in (None, ""):
            raise _schema(f"missing {field}")
    event_class = event["event_class"]
    nullable = set(spec["nullable"].get(event_class, []))
    for field in spec["classes"][event_class]:
        if field not in event:
            raise _schema(f"missing {field}")
        if event[field] is None and field not in nullable:
            raise _schema(f"{field} cannot be null")
    if event_class == "deployment" and event.get("previous_healthy_sha") is None:
        if event.get("install_kind") != "first_install":
            raise ControlError("EVIDENCE_MISSING", 409, "previous_healthy_sha is required after first install")
    if type(event["stream_epoch"]) is not int or event["stream_epoch"] < 1:
        raise _schema("stream_epoch must start at 1")
    if type(event["producer_seq"]) is not int or event["producer_seq"] < 1:
        raise _schema("producer_seq must be a positive integer")
    channel = _channel(event["stream_id"])
    if event["stream_id"] != f"{event['producer_id']}:{channel}":
        raise _schema("stream_id must be producer_id:channel")
    if channel not in spec["stream"]["channels"]:
        raise _schema("stream channel is not in the contract")
    if event_class == "evidence":
        if channel == "authorization":
            raise _schema("evidence cannot use the authorization channel")
    elif event_class != channel:
        raise _schema("event_class does not match the stream channel")
    registered = spec["event_types"].get(event["event_type"])
    if registered and registered["class"] != event_class:
        raise _schema("event_type does not match its class")
    _strict_types(event)
    _known_event_type(event)
    if event["event_type"] == "delivery.intent.recorded":
        note = event.get("note")
        prefix = "w05-intent-v1:"
        if (not isinstance(note, str) or not note.startswith(prefix)
                or len(note) != len(prefix) + 64 or any(c not in _SHA for c in note[len(prefix):])
                or event.get("evidence_ref") != note
                or channel != "task" or event.get("producer_id") != "w05"
                or not isinstance(event.get("app_id"), str) or not event["app_id"]
                or not isinstance(event.get("task_id"), str) or not event["task_id"]
                or not isinstance(event.get("run_id"), str) or not event["run_id"]):
            raise _schema("invalid isolated W05 intent evidence")
    if event.get("epoch_closed") and type(event.get("closed_through_seq")) is not int:
        raise _schema("epoch close requires closed_through_seq")
    channels = event.get("delivery_channels")
    if channels is not None:
        if not isinstance(channels, list) or not channels:
            raise _schema("delivery_channels must be a non-empty list")
        if any(type(item) is not str or not item.isidentifier() for item in channels):
            raise _schema("delivery channel name is invalid")


def _known_event_type(event: dict) -> None:
    event_type = event.get("event_type")
    _text(event_type, "event_type")
    spec = event_spec()
    registered = spec["event_types"].get(event_type)
    if registered is not None:
        if registered["class"] != event["event_class"]:
            raise _schema("event_type does not match its class")
        return
    expected = STRUCTURAL_TYPES.get(event_type)
    if expected is None or expected != event["event_class"]:
        raise _schema("event_type is not registered")


def _strict_types(event: dict) -> None:
    for field in ("event_id", "event_class", "repo_id", "producer_id", "occurred_at", "idempotency_key"):
        _text(event.get(field), field)
    _integer(event.get("stream_epoch"), "stream_epoch", minimum=1)
    _integer(event.get("producer_seq"), "producer_seq", minimum=1)
    for field in ("revision", "attempt", "policy_revision", "closed_through_seq"):
        if field in event and event[field] is not None:
            _integer(event[field], field, minimum=0)
    if "state" in event and event["state"] is not None:
        _text(event["state"], "state")
    if "stage" in event and event["stage"] is not None:
        _text(event["stage"], "stage")
    if "host_id" in event and event["host_id"] is not None:
        _text(event["host_id"], "host_id")
    if "app_id" in event and event["app_id"] is not None:
        _text(event["app_id"], "app_id")
    if "successor_producer" in event and event["successor_producer"] is not None:
        _text(event["successor_producer"], "successor_producer")
    if "note" in event and event["note"] is not None:
        _text(event["note"], "note")
    if "guard" in event and event["guard"] is not None:
        _text(event["guard"], "guard")
    if "progress_status" in event and event["progress_status"] is not None:
        _text(event["progress_status"], "progress_status")
    if "classification" in event and event["classification"] is not None:
        _text(event["classification"], "classification")
    if "epoch_closed" in event and type(event["epoch_closed"]) is not bool:
        raise _schema("epoch_closed must be a boolean")
    if "trusted_source" in event and event["trusted_source"] is not None:
        _text(event["trusted_source"], "trusted_source")
    event_class = event["event_class"]
    sha_fields = {
        "deployment": ("head", "base", "merge", "tree", "running_sha"),
        "authorization": ("head", "base", "merge", "tree"),
        "candidate": ("head", "base", "merge", "tree"),
    }.get(event_class, ())
    for field in sha_fields:
        if event.get(field) is not None:
            _sha(event[field], field)
    if event_class == "deployment" and event.get("previous_healthy_sha") is not None:
        _sha(event["previous_healthy_sha"], "previous_healthy_sha")
    if event_class == "deployment" and event.get("install_kind") not in {"first_install", "upgrade"}:
        raise _schema("install_kind is invalid")
    digest = event.get("artifact_digest")
    if event_class == "deployment":
        if type(digest) is not str or not digest.startswith("sha256:") or len(digest) != 7 + 64:
            raise _schema("artifact_digest is invalid")
        if any(char not in _SHA for char in digest[7:]):
            raise _schema("artifact_digest is invalid")


def _channel(stream_id: str) -> str:
    if type(stream_id) is not str or stream_id.count(":") != 1:
        raise _schema("stream_id must be producer_id:channel")
    return stream_id.split(":", 1)[1]


def authorize_source(event: dict, server_source: str | None = None) -> bool:
    """Server source is the only authority. The client field cannot grant trust.

    A server source that is real but is not the type's authority is an
    observation: it is stored as history and is not authoritative. A client
    that claims some other source is rejected. ``run.started`` is authoritative
    only for the server source ``agent``. Closing an authorization epoch is
    authoritative only for ``policy_service``.
    """
    registered = event_spec()["event_types"].get(event["event_type"])
    claimed = event.get("trusted_source")
    if server_source in (None, ""):
        if claimed not in (None, ""):
            raise ControlError("NOT_AUTHORIZED", 403, "client trusted source is not authority")
        return False
    if claimed not in (None, "", server_source):
        raise ControlError("NOT_AUTHORIZED", 403, "trusted source does not match the server principal")
    event_type = event.get("event_type")
    if event_type == "run.started":
        return server_source == "agent"
    if event_type == "authorization.epoch_closed":
        return server_source == "policy_service"
    if event_type == "delivery.intent.recorded":
        return server_source == "app_service"
    if registered is None:
        return False
    if server_source != registered["trusted_source"]:
        return False
    return True


def channel_of(event: dict) -> str:
    return event["stream_id"].split(":", 1)[1]
