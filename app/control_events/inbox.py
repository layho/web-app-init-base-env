"""Per-stream apply rules. A gap or an authorization epoch gate holds only that stream."""

HOLD_GAP = "GAP"
HOLD_EPOCH = "EPOCH_GATE"


def decide(event: dict, watermarks: dict, sealed: set, *, later: bool = False) -> tuple[str, str | None]:
    stream_id = event["stream_id"]
    epoch = event["stream_epoch"]
    seq = event["producer_seq"]
    channel = stream_id.split(":", 1)[1]
    key = (stream_id, epoch)
    info = watermarks.get(key, {"seq": 0, "closed": False})
    watermark = info["seq"]
    classification = event.get("classification")
    closing = bool(event.get("epoch_closed"))

    if classification == "never_existed" and (channel == "authorization" or event.get("event_class") != "diagnostic"):
        return "reject", "EVIDENCE_MISSING"
    if info.get("closed") or key in sealed:
        return "reject", "EVIDENCE_MISSING"
    if closing:
        through = event.get("closed_through_seq")
        if type(through) is not int or through != seq or later or watermark != seq - 1:
            return "reject", "EVIDENCE_MISSING"
        return "apply", None
    if channel == "authorization" and epoch > 1:
        previous = watermarks.get((stream_id, epoch - 1), {"seq": 0, "closed": False})
        if not previous.get("closed"):
            return "hold", HOLD_EPOCH
    if seq != watermark + 1:
        return "hold", HOLD_GAP
    return "apply", None
