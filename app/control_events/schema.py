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
)


def _schema(message: str) -> ControlError:
    return ControlError("SCHEMA_UNSUPPORTED", 422, message)


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
    if event.get("epoch_closed") and type(event.get("closed_through_seq")) is not int:
        raise _schema("epoch close requires closed_through_seq")
    channels = event.get("delivery_channels")
    if channels is not None:
        if not isinstance(channels, list) or not channels:
            raise _schema("delivery_channels must be a non-empty list")
        if any(type(item) is not str or not item.isidentifier() for item in channels):
            raise _schema("delivery channel name is invalid")


def _channel(stream_id: str) -> str:
    if type(stream_id) is not str or stream_id.count(":") != 1:
        raise _schema("stream_id must be producer_id:channel")
    return stream_id.split(":", 1)[1]


def authorize_source(event: dict) -> bool:
    """Trusted-source check. False means the event is a non-authoritative observation."""
    registered = event_spec()["event_types"].get(event["event_type"])
    source = event.get("trusted_source")
    if registered is None:
        if source:
            raise ControlError("NOT_AUTHORIZED", 403, "unregistered event cannot claim a trusted source")
        return False
    if source != registered["trusted_source"]:
        raise ControlError("NOT_AUTHORIZED", 403, "trusted source does not match the event type")
    return True


def channel_of(event: dict) -> str:
    return event["stream_id"].split(":", 1)[1]
