"""Per-stream apply rules. A gap or an authorization epoch gate holds only that stream."""

HOLD_GAP = "GAP"
HOLD_EPOCH = "EPOCH_GATE"


def decide(event: dict, watermarks: dict, sealed: set, *, later: bool = False) -> tuple[str, str | None]:
    stream_id = event["stream_id"]
    epoch = event["stream_epoch"]
    seq = event["producer_seq"]
    channel = stream_id.split(":", 1)[1]
    key = (stream_id, epoch)
    watermark = watermarks.get(key, {"seq": 0, "closed": False})["seq"]
    classification = event.get("classification")
    closing = bool(event.get("epoch_closed"))

    if classification == "never_existed" and (channel == "authorization" or event.get("event_class") != "diagnostic"):
        return "reject", "EVIDENCE_MISSING"
    if closing and (event.get("closed_through_seq") != seq or later):
        return "reject", "EVIDENCE_MISSING"
    if key in sealed:
        complete_close = closing and event.get("closed_through_seq") == seq and seq == watermark + 1
        if not complete_close:
            return "reject", "EVIDENCE_MISSING"
        return "apply", None
    if channel == "authorization" and epoch > 1:
        previous = watermarks.get((stream_id, epoch - 1), {"seq": 0, "closed": False})
        if not previous.get("closed"):
            return "hold", HOLD_EPOCH
    if seq != watermark + 1:
        return "hold", HOLD_GAP
    return "apply", None
