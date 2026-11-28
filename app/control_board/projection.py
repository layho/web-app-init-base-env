"""Read-model projection for the unified task board.

Facts come only from sequenced events. A diagnostic observation cannot
replace an authorization, deployment, or candidate fact. A caller-supplied
success flag is not authority. Missing fields stay explicit.
"""

from __future__ import annotations

from .flags import PHASES

# Real W00 event types, plus the two structural types W01 accepts.
# A client source string is not an entry in this table.
W00_EVENT_TYPES = {
    "authorization.granted": ("authorization", "policy_service"),
    "authorization.revoked": ("authorization", "policy_service"),
    "deployment.health": ("deployment", "host_deployer"),
    "deployment.commit": ("deployment", "host_deployer"),
    "ci.test.finished": ("evidence", "macci"),
    "diagnostic.observation": ("diagnostic", "hostops_readonly"),
    "run.started": ("task", "agent"),
    "authorization.epoch_closed": ("authorization", "policy_service"),
}
# W00 has no candidate event type. This projection accepts one only when a
# trusted W01 read receipt carries verified or accepted evidence.
EVIDENCED_READ_TYPES = {
    "candidate.registered": ("candidate", "policy_service"),
}
TRUSTED_SOURCE = {name: source for name, (_kind, source) in W00_EVENT_TYPES.items()}
EXECUTE_OPERATIONS = frozenset({"run.execute", "task.execute"})
LANES = {
    "development": ("develop", "review"),
    "test": ("test",),
    "merge": ("integrate", "preview_merge"),
    "deploy": ("deploy",),
}
SUCCESS_STATES = frozenset({"SUCCEEDED", "COMMIT", "INTEGRATED", "PUBLISHED", "SATISFIED", "PASSED"})
FAIL_STATES = frozenset({"FAILED", "REJECTED", "ERROR", "FAILURE"})
PROGRESS_STATES = frozenset({"RUNNING", "LEASED", "PENDING", "TESTING", "OPEN", "INTEGRATING", "DEPLOYING", "ACCEPTING"})
SUCCESS_WORDS = ("success", "passed", "succeeded", "commit", "ok", "healthy")


def reject_reason(event):
    if not isinstance(event, dict):
        return "event 不是对象"
    if event.get("schema_version") != 2:
        return "schema_version 不受支持"
    receipt_problem = _received_at_conflict(event)
    if receipt_problem:
        return receipt_problem
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        return "event_id 缺失"
    if not isinstance(event.get("stream_id"), str) or ":" not in event["stream_id"]:
        return "stream_id 缺失"
    epoch = event.get("stream_epoch", 1)
    if isinstance(epoch, bool) or type(epoch) is not int or epoch < 1:
        return "stream_epoch 无效"
    seq = event.get("producer_seq")
    if isinstance(seq, bool) or type(seq) is not int or seq < 1:
        return "producer_seq 无效"
    revision = event.get("revision", 0)
    if isinstance(revision, bool) or type(revision) is not int:
        return "revision 无效"
    return None


def _read_receipt(event):
    receipt = event.get("read_receipt")
    if not isinstance(receipt, dict):
        return None
    if receipt.get("kind") != "read" or receipt.get("authority") != "W01":
        return None
    if receipt.get("event_id") not in (None, event.get("event_id")):
        return None
    return receipt


def _received_at_conflict(event):
    """Caller received_at is kept only when it matches a W01 read receipt.

    A submit receipt is not that read receipt. A missing receipt, a submit
    receipt, or a different server timestamp is a contradiction.
    """
    if "received_at" not in event:
        return None
    receipt = _read_receipt(event)
    if receipt is None or receipt.get("received_at") != event.get("received_at"):
        return "received_at 与 W01 服务端回执矛盾"
    submit = event.get("submit_receipt")
    if isinstance(submit, dict) and submit.get("kind") == "submit":
        if submit.get("received_at") not in (None, receipt.get("received_at")):
            return "received_at 与 W01 服务端回执矛盾"
    return None


def authoritative(event):
    """Authority comes from a W01 read receipt and accepted evidence.

    ``source`` and ``trusted_source`` are caller strings. The word
    ``authoritative`` is one of those strings. Neither one proves authority.
    A submit receipt records an append; it is not a trusted read.
    """
    if not isinstance(event, dict):
        return False
    receipt = _read_receipt(event)
    if receipt is None:
        return False
    if isinstance(event.get("submit_receipt"), dict) and event["submit_receipt"].get("kind") == "submit":
        if event["submit_receipt"].get("received_at") not in (None, receipt.get("received_at")):
            return False
    fact_state = receipt.get("fact_state") or event.get("fact_state")
    if fact_state != "ACCEPTED":
        return False
    evidence = receipt.get("evidence") if receipt.get("evidence") in {"verified", "accepted"} else event.get("evidence")
    if evidence not in {"verified", "accepted"}:
        return False
    server_source = receipt.get("server_source")
    if not isinstance(server_source, str) or not server_source or server_source == "authoritative":
        return False
    spec = W00_EVENT_TYPES.get(event.get("event_type"))
    if spec is None:
        spec = EVIDENCED_READ_TYPES.get(event.get("event_type"))
    if spec is None:
        return False
    expected_class, expected_source = spec
    if event.get("event_class") != expected_class or server_source != expected_source:
        return False
    claimed = event.get("trusted_source")
    if isinstance(claimed, str) and claimed not in ("", server_source):
        return False
    return True


def fold(events):
    """Sequence each stream. Gaps, authorization holes, and bad events stay visible."""
    rejected = []
    usable = []
    for event in events:
        reason = reject_reason(event)
        if reason:
            rejected.append({"event_id": event.get("event_id") if isinstance(event, dict) else None, "reason": reason})
            continue
        usable.append(event)
    grouped = {}
    for event in usable:
        grouped.setdefault(event["stream_id"], {}).setdefault(event.get("stream_epoch", 1), []).append(event)
    applied = []
    held = []
    for stream_id in sorted(grouped):
        channel = stream_id.rsplit(":", 1)[-1]
        closed = set()
        for epoch in sorted(grouped[stream_id]):
            batch = sorted(grouped[stream_id][epoch], key=lambda item: item["producer_seq"])
            if channel == "authorization" and epoch > 1 and (epoch - 1) not in closed:
                for event in batch:
                    held.append(_held(event, "authorization_epoch"))
                continue
            watermark = 0
            stopped = False
            seen_seq = {}
            for event in batch:
                if stopped:
                    held.append(_held(event, "gap"))
                    continue
                seq = event["producer_seq"]
                prior = seen_seq.get(seq)
                if prior is not None and prior != event.get("event_id"):
                    held.append(_held(event, "same_seq"))
                    continue
                seen_seq[seq] = event.get("event_id")
                if seq <= watermark:
                    held.append(_held(event, "duplicate"))
                    continue
                if seq != watermark + 1:
                    held.append(_held(event, "gap"))
                    stopped = True
                    continue
                if event.get("classification") == "never_existed" and channel == "authorization":
                    held.append(_held(event, "never_existed"))
                    stopped = True
                    continue
                if event.get("classification") == "never_existed" and channel == "diagnostic":
                    watermark = seq
                    if event.get("epoch_closed") is True:
                        closed.add(epoch)
                    continue
                applied.append(event)
                watermark = seq
                if event.get("epoch_closed") is True:
                    closed.add(epoch)
    return {"applied": applied, "held": held, "rejected": rejected, "gap": any(item["reason"] != "duplicate" for item in held)}


def project(events, schedule=None, shells=()):
    folded = fold(events)
    facts = {}
    advisory = []
    applied_by_task = {}
    for event in folded["applied"]:
        if _diagnostic(event):
            advisory.append(event)
            task_id = _task_id(event)
            if task_id and _app_id(event):
                applied_by_task.setdefault((event["app_id"], task_id), [])
            continue
        key = _fact_key(event)
        if key is None:
            continue
        _put(facts, key, event)
        task_id = _task_id(event)
        app_id = _app_id(event)
        if task_id and app_id:
            applied_by_task.setdefault((app_id, task_id), []).append(event)
    schedule = schedule or {}
    runtime = schedule.get("runtime_evidence") is True
    synthetic = schedule.get("test_double") is True and not runtime
    tasks = {}
    for (app_id, task_id), bucket in applied_by_task.items():
        tasks[(app_id, task_id)] = _task_view(app_id, task_id, bucket, facts, advisory, schedule, synthetic, runtime)
    for app_id, task_id in shells:
        if isinstance(app_id, str) and isinstance(task_id, str):
            tasks.setdefault((app_id, task_id), _task_view(app_id, task_id, [], facts, advisory, schedule, synthetic, runtime))
    for row in schedule.get("tasks") or []:
        app_id = row.get("app_id")
        task_id = row.get("task_id")
        if not isinstance(app_id, str) or not isinstance(task_id, str):
            continue
        tasks.setdefault((app_id, task_id), _task_view(app_id, task_id, [], facts, advisory, schedule, synthetic, runtime))
    for view in tasks.values():
        _attach_schedule(view, schedule, synthetic, runtime)
    return {
        "tasks": tasks,
        "held": folded["held"],
        "rejected": folded["rejected"],
        "gap": folded["gap"],
        "applied_ids": [event["event_id"] for event in folded["applied"]],
    }


def _task_view(app_id, task_id, bucket, facts, advisory, schedule, synthetic, runtime):
    del schedule, runtime
    stages = {}
    for phase in PHASES:
        fact = facts.get(("stage", app_id, task_id, phase))
        stages[phase] = _stage(fact)
    lanes = {}
    failure_reasons = []
    for name, phases in LANES.items():
        lane, failure = _lane(app_id, task_id, name, phases, facts)
        lanes[name] = lane
        if failure:
            failure_reasons.append(failure)
    tests = _tests(app_id, task_id, facts)
    task_events = [event for event in bucket if event.get("event_class") == "task"]
    owner_event = _best(task_events, "owner")
    mode_event = _best(task_events, "schedule_mode")
    executor = _executor(task_events, facts, app_id, task_id)
    repo = _best(bucket, "repo_id")
    prs = _prs(app_id, task_id, facts)
    advisory_items = [_advisory_item(event) for event in advisory if _task_id(event) == task_id and _app_id(event) == app_id]
    advisory_failures = []
    success_ignored = False
    for item in advisory_items:
        if item["success_claim_ignored"]:
            success_ignored = True
        if item["failure_reason"]:
            advisory_failures.append({
                "text": item["failure_reason"],
                "display": item["failure_reason"],
                "evidence": "unknown",
                "authoritative": False,
                "source": "hostops_readonly",
            })
    archived, archive_reason = _archive(prs)
    return {
        "task_id": task_id,
        "app_id": app_id,
        "repo_id": None if repo is None else repo.get("repo_id"),
        "repo_evidence": "missing" if repo is None else "present",
        "owner": _field(None if owner_event is None else owner_event.get("owner"), owner_event),
        "executor": executor,
        "schedule_mode": _mode(mode_event),
        "stages": stages,
        "lanes": lanes,
        "tests": tests,
        "prs": prs,
        "failure_reasons": failure_reasons,
        "advisory_failures": advisory_failures,
        "advisory": {"items": advisory_items, "success_claim_ignored": success_ignored},
        "unknowns": _unknowns(stages, lanes, executor),
        "next_step": _next_step(stages),
        "closure": _closure(stages, prs),
        "archived": archived,
        "archive_reason": archive_reason,
        "schedule_evidence": "missing",
        "projection_only": True,
        "authoritative_store": False,
        "intervention": {"entry": "manual", "connected_executor": False},
        "history": [_history_item(event) for event in bucket],
    }


def _attach_schedule(view, schedule, synthetic, runtime):
    chains = [
        row for row in (schedule.get("chains") or [])
        if row.get("task_id") == view["task_id"] and (row.get("app_id") in (None, view["app_id"]))
    ]
    by_candidate = {row.get("candidate_id"): row for row in chains if row.get("candidate_id")}
    seen = {item["candidate_id"] for item in view["prs"]}
    for item in view["prs"]:
        row = by_candidate.get(item["candidate_id"])
        if row:
            _copy_schedule(item, row, synthetic, runtime)
    for row in chains:
        candidate_id = row.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in seen:
            continue
        claimed = row.get("state") if isinstance(row.get("state"), str) else None
        item = {
            "candidate_id": candidate_id,
            "pr_number": row.get("pr_number") if type(row.get("pr_number")) is int else None,
            "pr_evidence": "present" if type(row.get("pr_number")) is int else "missing",
            "raw_state": None,
            "authoritative_state": "unrecorded",
            "claimed_state": claimed,
            "evidence": "synthetic" if synthetic else "missing",
            "authoritative": False,
            "from_event": False,
            "association": "explicit_task_id",
            "task_id": view["task_id"],
            "archived": False,
            "head": None,
            "base": None,
            "merge": None,
            "tree": None,
            "tuple_evidence": "missing",
            "priority": None,
            "dependencies": None,
            "priority_evidence": "missing",
            "dependency_evidence": "missing",
        }
        _copy_schedule(item, row, synthetic, runtime)
        view["prs"].append(item)
    view["prs"] = sorted(view["prs"], key=_pr_sort)
    if synthetic and ((schedule.get("tasks") or chains)):
        view["schedule_evidence"] = "synthetic"
    elif runtime:
        view["schedule_evidence"] = "present"
    else:
        view["schedule_evidence"] = "missing"
    view["archived"], view["archive_reason"] = _archive(view["prs"])


def _copy_schedule(item, row, synthetic, runtime):
    evidence = "present" if runtime else ("synthetic" if synthetic else "missing")
    if "priority" in row and type(row.get("priority")) is int and not isinstance(row.get("priority"), bool):
        item["priority"] = row["priority"]
        item["priority_evidence"] = evidence
    else:
        item["priority"] = None
        item["priority_evidence"] = "missing"
    if isinstance(row.get("dependencies"), list):
        item["dependencies"] = list(row["dependencies"])
        item["dependency_evidence"] = evidence
    else:
        item["dependencies"] = None
        item["dependency_evidence"] = "missing"
    if item.get("claimed_state") is None and isinstance(row.get("state"), str):
        item["claimed_state"] = row["state"]


def _stage(fact):
    if fact is None:
        return {"at": None, "evidence": "missing", "raw_state": None, "authoritative": False, "received_at": None, "received_at_evidence": "missing"}
    event = fact["event"]
    occurred = event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else None
    if fact["authoritative"] and occurred:
        evidence = "present"
    else:
        evidence = "unknown"
    return {
        "at": occurred if fact["authoritative"] else None,
        "evidence": evidence,
        "raw_state": event.get("state") if isinstance(event.get("state"), str) else None,
        "authoritative": fact["authoritative"],
        "received_at": None,
        "received_at_evidence": "missing",
    }


def _lane(app_id, task_id, name, phases, facts):
    chosen = None
    for phase in phases:
        fact = facts.get(("stage", app_id, task_id, phase))
        if fact is not None and (chosen is None or _better(fact["event"], chosen["event"])):
            chosen = fact
    if name == "test":
        for key, fact in facts.items():
            if key[0] == "test" and key[1] == app_id and key[2] == task_id and (chosen is None or _better(fact["event"], chosen["event"])):
                chosen = fact
    if name == "deploy":
        fact = facts.get(("deploy", app_id, task_id))
        if fact is not None and (chosen is None or _better(fact["event"], chosen["event"])):
            chosen = fact
    if chosen is None:
        return {
            "state": "unrecorded",
            "raw_state": None,
            "at": None,
            "evidence": "missing",
            "failure_reason": None,
            "authoritative": False,
        }, None
    event = chosen["event"]
    raw = event.get("state") if isinstance(event.get("state"), str) else None
    reason = event.get("failure_reason") if isinstance(event.get("failure_reason"), str) and event.get("failure_reason") else None
    at = event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else None
    failure = None
    if not chosen["authoritative"]:
        state = "unknown"
        evidence = "unknown"
    else:
        evidence = "present"
        if raw in FAIL_STATES:
            state = "failed"
            if reason is None:
                failure = {"text": "未记录失败原因", "display": "未记录失败原因", "evidence": "missing", "authoritative": True, "source": event.get("source")}
            else:
                failure = {"text": reason, "display": reason, "evidence": "present", "authoritative": True, "source": event.get("source")}
        elif raw in SUCCESS_STATES:
            state = "recorded"
        elif raw in PROGRESS_STATES:
            state = "in_progress"
        else:
            state = "unknown"
    return {
        "state": state,
        "raw_state": raw,
        "at": at if chosen["authoritative"] else None,
        "evidence": evidence,
        "failure_reason": reason if reason else ("未记录失败原因" if state == "failed" else None),
        "authoritative": chosen["authoritative"],
    }, failure


def _tests(app_id, task_id, facts):
    rows = []
    for key, fact in facts.items():
        if key[0] != "test" or key[1] != app_id or key[2] != task_id:
            continue
        event = fact["event"]
        raw = event.get("state") if isinstance(event.get("state"), str) else None
        if not fact["authoritative"]:
            state = "unknown"
        elif raw in FAIL_STATES:
            state = "failed"
        elif raw in SUCCESS_STATES:
            state = "recorded"
        else:
            state = "unknown"
        reason = event.get("failure_reason") if isinstance(event.get("failure_reason"), str) and event.get("failure_reason") else None
        rows.append({
            "task_id": task_id,
            "candidate_id": key[3] if isinstance(key[3], str) else None,
            "state": state,
            "failure_reason": reason if reason else ("未记录失败原因" if state == "failed" else None),
            "evidence": "present" if fact["authoritative"] else "unknown",
            "authoritative": fact["authoritative"],
            "archived": False,
        })
    return rows


def _executor(bucket, facts, app_id, task_id):
    verified = None
    for key, fact in facts.items():
        if key[0] != "authorization" or key[1] != app_id or key[2] != task_id:
            continue
        event = fact["event"]
        if not fact["authoritative"] or event.get("operation") not in EXECUTE_OPERATIONS:
            continue
        if event.get("state") not in {"GRANTED", "SUCCEEDED"}:
            continue
        name = event.get("executor") if isinstance(event.get("executor"), str) and event.get("executor") else None
        if name and (verified is None or _better(event, verified)):
            verified = event
    if verified is not None:
        return {"claimed": verified.get("executor"), "verified": True, "evidence": "present", "code": None, "display": verified.get("executor")}
    claim = _best(bucket, "executor")
    claimed = None if claim is None else claim.get("executor")
    if not isinstance(claimed, str) or not claimed:
        claimed = None
    return {
        "claimed": claimed,
        "verified": False,
        "evidence": "unknown" if claimed else "missing",
        "code": "UNKNOWN_EXECUTOR",
        "display": "未接通执行器" if claimed is None else claimed,
    }


def _prs(app_id, task_id, facts):
    rows = []
    for key, fact in facts.items():
        if key[0] != "candidate" or key[1] != app_id or key[2] != task_id:
            continue
        event = fact["event"]
        number = event.get("pr_number") if type(event.get("pr_number")) is int and not isinstance(event.get("pr_number"), bool) else None
        raw = event.get("state") if isinstance(event.get("state"), str) else None
        rows.append({
            "candidate_id": key[3],
            "pr_number": number,
            "pr_evidence": "present" if number is not None else "missing",
            "raw_state": raw if fact["authoritative"] else None,
            "authoritative_state": raw if fact["authoritative"] else "unrecorded",
            "claimed_state": raw,
            "evidence": "present" if fact["authoritative"] else "unknown",
            "authoritative": fact["authoritative"],
            "from_event": True,
            "association": "explicit_task_id",
            "task_id": task_id,
            "archived": fact["authoritative"] and raw == "SUPERSEDED",
            "head": _sha(event.get("head")),
            "base": _sha(event.get("base")),
            "merge": _sha(event.get("merge")),
            "tree": _sha(event.get("tree")),
            "tuple_evidence": "present" if all(_sha(event.get(name)) for name in ("head", "base", "merge", "tree")) else "missing",
            "priority": None,
            "dependencies": None,
            "priority_evidence": "missing",
            "dependency_evidence": "missing",
        })
    return sorted(rows, key=_pr_sort)


def _archive(prs):
    authoritative = [item for item in prs if item.get("from_event") and item.get("authoritative")]
    if authoritative and all(item.get("raw_state") == "SUPERSEDED" for item in authoritative):
        return True, "superseded"
    return False, None


def _closure(stages, prs):
    published = [item for item in prs if item.get("authoritative") and item.get("raw_state") == "PUBLISHED"]
    close = stages["close"]
    if close["authoritative"] and close["raw_state"] == "SUCCEEDED" and published and len(published) == len([item for item in prs if item.get("authoritative")]):
        return {"closed": True, "evidence": "present", "reason": "任务级关闭证据已记录。"}
    return {
        "closed": False,
        "evidence": "missing",
        "reason": "未关闭：SATISFIED 不是关闭。缺少每条必要链的正式发布证据或任务级验收。",
    }


def _next_step(stages):
    for phase in PHASES:
        if stages[phase]["evidence"] == "missing":
            return {"phase": phase, "evidence": "missing", "actionable": False, "display": "下一步没有权威证据"}
    return {"phase": None, "evidence": "missing", "actionable": False, "display": "下一步没有权威证据"}


def _unknowns(stages, lanes, executor):
    found = []
    if executor["code"] == "UNKNOWN_EXECUTOR":
        found.append("执行者未验证")
    for phase, stage in stages.items():
        if stage["evidence"] == "missing":
            found.append(f"{phase} 未记录")
    for name, lane in lanes.items():
        if lane["evidence"] == "missing":
            found.append(f"{name} 未记录")
    return found


def _field(value, event):
    if not isinstance(value, str) or not value:
        return {"value": None, "evidence": "missing", "display": "未记录"}
    if event is not None and authoritative(event):
        return {"value": value, "evidence": "present", "display": value}
    return {"value": value, "evidence": "unknown", "display": value}


def _mode(event):
    if event is None:
        return {"value": None, "evidence": "missing", "display": "未记录"}
    value = event.get("schedule_mode")
    if value not in {"realtime", "scheduled"}:
        return {"value": None, "evidence": "missing", "display": "未记录"}
    if authoritative(event):
        return {"value": value, "evidence": "present", "display": "实时" if value == "realtime" else "定时"}
    return {"value": value, "evidence": "unknown", "display": "未记录"}


def _history_item(event):
    return {
        "event_id": event["event_id"],
        "stage": event.get("stage") if event.get("stage") in PHASES else None,
        "state": event.get("state") if isinstance(event.get("state"), str) else None,
        "occurred_at": event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else None,
        "received_at": None,
        "received_at_evidence": "missing",
        "revision": event.get("revision", 0),
        "authoritative": authoritative(event),
        "archived": event.get("state") == "SUPERSEDED",
    }


def _advisory_item(event):
    reason = event.get("failure_reason") if isinstance(event.get("failure_reason"), str) else None
    return {
        "event_id": event["event_id"],
        "stream_id": event["stream_id"],
        "failure_reason": reason,
        "success_claim_ignored": _success_claim(event),
        "authoritative": False,
    }


def _success_claim(event):
    if event.get("state") in SUCCESS_STATES:
        return True
    blob = " ".join(str(event.get(name) or "") for name in ("state", "failure_reason", "note")).lower()
    return any(word in blob for word in SUCCESS_WORDS)


def _fact_key(event):
    app_id = _app_id(event)
    task_id = _task_id(event)
    if not app_id or not task_id:
        return None
    kind = event.get("event_class")
    if kind == "task" and event.get("stage") in PHASES:
        return ("stage", app_id, task_id, event["stage"])
    if kind == "candidate" and isinstance(event.get("candidate_id"), str):
        return ("candidate", app_id, task_id, event["candidate_id"])
    if event.get("event_type") == "ci.test.finished":
        return ("test", app_id, task_id, event.get("candidate_id"))
    if kind == "deployment":
        return ("deploy", app_id, task_id)
    if kind == "authorization" and isinstance(event.get("operation"), str):
        return ("authorization", app_id, task_id, event["operation"], event.get("candidate_id"))
    return None


def _put(facts, key, event):
    incoming = {"event": event, "authoritative": authoritative(event), "revision": event.get("revision", 0)}
    current = facts.get(key)
    if current is None or _better(event, current["event"]):
        facts[key] = incoming
        return
    if incoming["revision"] == current["revision"] and event.get("state") != current["event"].get("state"):
        current["conflict"] = True


def _better(event, current):
    if event.get("revision", 0) != current.get("revision", 0):
        return event.get("revision", 0) > current.get("revision", 0)
    return authoritative(event) and not authoritative(current)


def _diagnostic(event):
    return event.get("event_class") == "diagnostic" or event.get("event_type") == "diagnostic.observation"


def _task_id(event):
    task_id = event.get("task_id") or event.get("subject_id")
    return task_id if isinstance(task_id, str) and task_id else None


def _app_id(event):
    app_id = event.get("app_id")
    return app_id if isinstance(app_id, str) and app_id else None


def _best(events, field):
    chosen = None
    for event in events:
        value = event.get(field)
        if not isinstance(value, str) or not value:
            continue
        if field == "schedule_mode" and value not in {"realtime", "scheduled"}:
            continue
        if chosen is None or _better(event, chosen):
            chosen = event
    return chosen


def _held(event, reason):
    return {
        "event_id": event.get("event_id"),
        "stream_id": event.get("stream_id"),
        "stream_epoch": event.get("stream_epoch", 1),
        "producer_seq": event.get("producer_seq"),
        "reason": reason,
        "task_id": _task_id(event),
    }


def _sha(value):
    if isinstance(value, str) and len(value) == 40 and all(char in "0123456789abcdef" for char in value):
        return value
    return None


def _pr_sort(item):
    number = item.get("pr_number")
    if type(number) is int:
        return (0, number, item.get("candidate_id") or "")
    return (1, 0, item.get("candidate_id") or "")
