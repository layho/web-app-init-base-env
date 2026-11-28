"""Stage journal with compare-and-swap, idempotency, and epoch restart."""

import copy
import json
import os
import threading


class Journal:
    def __init__(self, path, faults):
        self.path = path
        self.faults = faults
        self.lock = threading.Lock()
        self.data = self._load()

    def inspect(self):
        with self.lock:
            return self._public(self.data)

    def transact(self, *, expected_revision, principal, scope, operation, idempotency_key, payload_hash, run_id, handler, result_for):
        idem_id = f"{principal}|{scope}|{operation}|{idempotency_key}"
        with self.lock:
            record = self.data["idempotency"].get(idem_id)
            if record is not None:
                if record["hash"] != payload_hash:
                    return result_for("IDEMPOTENCY_PAYLOAD_MISMATCH", 409, False, "idempotency key was used with a different payload", {}, False, self.data, original=None)
                return result_for(
                    "IDEMPOTENCY_REPLAY",
                    200,
                    True,
                    "idempotent replay",
                    record["public"].get("plan") or {},
                    record["public"].get("simulated", False),
                    self.data,
                    original=record["public"],
                )
            if self.data["revision"] != expected_revision:
                return result_for("CONFLICT_REVISION", 409, False, "expected revision does not match the journal", {}, False, self.data, original=None)
            new_data = copy.deepcopy(self.data)
            outcome = handler(new_data)
            if "journal_fsync_fails" in self.faults:
                return result_for("JOURNAL_FAILED", 409, False, "journal write failed closed", outcome.get("plan") or {}, False, self.data, original=None)
            self._bookkeep(new_data, outcome, idem_id, payload_hash, operation, run_id)
            self._save(new_data)
            self.data = new_data
            return result_for(
                outcome["code"],
                outcome["http"],
                outcome["ok"],
                outcome["diagnosis"],
                outcome.get("plan") or {},
                outcome.get("simulated", False),
                new_data,
                original=None,
            )

    def _bookkeep(self, data, outcome, idem_id, payload_hash, operation, run_id):
        if outcome.get("count_seq", True):
            data["seq"] += 1
            epoch = str(data["epoch"])
            data["epochs"].setdefault(epoch, {"gap": False, "closed": False, "applied_seqs": []})
            data["epochs"][epoch]["applied_seqs"].append(data["seq"])
        if outcome.get("record_run", True) and run_id:
            runs = data["runs"].setdefault(run_id, [])
            if operation not in runs:
                runs.append(operation)
        data["revision"] += 1
        public = {
            "code": outcome["code"],
            "ok": outcome["ok"],
            "http": outcome["http"],
            "diagnosis": outcome["diagnosis"],
            "plan": outcome.get("plan") or {},
            "simulated": outcome.get("simulated", False),
        }
        data["idempotency"][idem_id] = {"hash": payload_hash, "public": public}

    def _load(self):
        if not self.path.exists():
            return self._empty()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data):
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(data, sort_keys=True, indent=2).encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                wrote = os.write(fd, view[offset:])
                if wrote <= 0:
                    raise OSError("short journal write")
                offset += wrote
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.path)

    def _empty(self):
        return {
            "revision": 0,
            "epoch": 1,
            "seq": 0,
            "epochs": {"1": {"gap": False, "closed": False, "applied_seqs": []}},
            "reservations": {},
            "resources": {},
            "runs": {},
            "idempotency": {},
            "deployment_state": "ABSENT",
            "service_generation": 0,
            "receipt": None,
        }

    def _public(self, data):
        return {
            "revision": data["revision"],
            "epoch": data["epoch"],
            "seq": data["seq"],
            "epochs": copy.deepcopy(data["epochs"]),
            "reservations": copy.deepcopy(data["reservations"]),
            "resources": copy.deepcopy(data["resources"]),
            "deployment_state": data["deployment_state"],
            "idempotency_count": len(data["idempotency"]),
            "service_generation": data["service_generation"],
        }
