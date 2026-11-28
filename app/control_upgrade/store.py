"""Local journal for upgrade plans. This is not a production database."""

import copy
import json
import os
from pathlib import Path


class Journal:
    def __init__(self, root=None):
        self.root = None if root is None else Path(root)
        self.idem_reads = 0
        self._state = {"plans": {}, "idempotency": {}, "cache": {}}
        if self.root is not None:
            self._load()

    def get_idem(self, key):
        self.idem_reads += 1
        record = self._state["idempotency"].get(key)
        return None if record is None else copy.deepcopy(record)

    def put_idem(self, key, record):
        self._state["idempotency"][key] = copy.deepcopy(record)
        self._flush()

    def get_plan(self, plan_id):
        plan = self._state["plans"].get(plan_id)
        return None if plan is None else copy.deepcopy(plan)

    def put_plan(self, plan):
        self._state["plans"][plan["plan_id"]] = copy.deepcopy(plan)
        self._flush()

    def plans_for(self, app_id):
        return [copy.deepcopy(plan) for plan in self._state["plans"].values() if plan.get("app_id") == app_id]

    def get_cache(self, app_id):
        snapshot = self._state["cache"].get(app_id)
        return None if snapshot is None else copy.deepcopy(snapshot)

    def put_cache(self, app_id, snapshot):
        self._state["cache"][app_id] = copy.deepcopy(snapshot)
        self._flush()

    def _load(self):
        path = self.root / "state.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return
        self._state["plans"] = data.get("plans") or {}
        self._state["idempotency"] = data.get("idempotency") or {}
        self._state["cache"] = data.get("cache") or {}

    def _flush(self):
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.root / ".state.json.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.root / "state.json")
        os.chmod(self.root / "state.json", 0o600)
