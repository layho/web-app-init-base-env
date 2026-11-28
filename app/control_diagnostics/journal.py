"""Durable diagnostic journal.

Records survive restart and rotation. A missing segment or a lost counter
is reported. Reads do not append. This journal is not an authority store.
"""

import json
import os
import stat
from pathlib import Path

from app.control_diagnostics.redaction import contains_secret, redact_value


class Limits:
    def __init__(self, max_records_per_segment=50, max_segment_bytes=1_000_000, max_log_bytes=65_536):
        self.max_records_per_segment = max_records_per_segment
        self.max_segment_bytes = max_segment_bytes
        self.max_log_bytes = max_log_bytes


def project_stream(records):
    """Apply producer_seq in order. A hole holds every later record."""
    ordered = sorted(records, key=lambda record: (record["producer_seq"], record["receipt_seq"]))
    watermark = 0
    pending = list(ordered)
    applied = []
    moved = True
    while moved:
        moved = False
        rest = []
        for record in pending:
            if record["producer_seq"] == watermark + 1:
                watermark = record["producer_seq"]
                applied.append(record)
                moved = True
            else:
                rest.append(record)
        pending = rest
    held = []
    duplicates = []
    for record in pending:
        if record["producer_seq"] <= watermark:
            duplicates.append(record)
        else:
            held.append(record)
    present = {record["producer_seq"] for record in held}
    max_seq = max(present) if present else watermark
    missing = [seq for seq in range(watermark + 1, max_seq + 1) if seq not in present]
    return watermark, applied, held, missing, duplicates


class Journal:
    def __init__(self, root, meta, writable, limits, counter_loss=False):
        self.root = Path(root)
        self.meta = meta
        self.writable = writable
        self.limits = limits
        self.counter_loss = counter_loss
        self._active_count = self._count_lines(self.active_path()) if self.active_path().exists() else 0

    @classmethod
    def open(cls, root, *, writable, limits=None):
        root = Path(root)
        limits = limits or Limits()
        meta_path = root / "meta.json"
        if writable:
            root.mkdir(mode=0o700, exist_ok=True)
            (root / "segments").mkdir(mode=0o700, exist_ok=True)
            os.chmod(root, 0o700)
            os.chmod(root / "segments", 0o700)
        if meta_path.exists():
            if not _regular(meta_path):
                raise ValueError("unsafe diagnostic meta")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return cls(root, meta, writable, limits, counter_loss=bool(meta.get("counter_loss")))
        if _data_files(root):
            if not writable:
                meta = _empty_meta(epoch=1)
                meta["journal_epoch"] = None
                meta["counter_loss"] = True
                meta["segments"] = _segment_numbers(root)
                return cls(root, meta, False, limits, counter_loss=True)
            epoch = _max_journal_epoch(root) + 1
            meta = _empty_meta(epoch=epoch)
            meta["counter_loss"] = True
            meta["segments"] = _segment_numbers(root)
            meta["active_segment"] = (max(meta["segments"]) + 1) if meta["segments"] else 1
            meta["next_receipt_seq"] = _max_receipt(root) + 1
            meta["idempotency"] = _recover_idempotency(root)
            journal = cls(root, meta, True, limits, counter_loss=True)
            journal._persist_meta()
            return journal
        if not writable:
            return cls(root, _empty_meta(epoch=1), False, limits, counter_loss=False)
        meta = _empty_meta(epoch=1)
        journal = cls(root, meta, True, limits, counter_loss=False)
        journal._persist_meta()
        return journal

    def active_path(self):
        return self.root / "active.jsonl"

    def status(self):
        return {
            "journal_epoch": self.meta.get("journal_epoch"),
            "next_local_producer_seq": self.meta.get("next_local_producer_seq"),
            "next_receipt_seq": self.meta.get("next_receipt_seq"),
            "counter_loss": self.counter_loss,
            "segments": list(self.meta.get("segments", [])),
            "side_effect": False,
        }

    def append(self, record):
        if not self.writable:
            raise PermissionError("read-only diagnostic journal")
        stored = redact_value(record)
        encoded = (json.dumps(stored, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        if contains_secret(encoded.decode("ascii")):
            raise ValueError("redaction failed closed")
        if self._active_count >= self.limits.max_records_per_segment:
            self._rotate()
        path = self.active_path()
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        self._active_count += 1
        self.meta["next_receipt_seq"] = stored["receipt_seq"] + 1
        scope = stored.get("idempotency_scope")
        if scope:
            self.meta.setdefault("idempotency", {})[scope] = {
                "hash": stored["payload_hash"],
                "record_id": stored["record_id"],
            }
        self._persist_meta()
        return stored

    def find_idempotency(self, scope):
        return (self.meta.get("idempotency") or {}).get(scope)

    def records(self):
        loaded = []
        missing_segments = []
        corrupt = 0
        indexed = list(self.meta.get("segments") or [])
        numbers = sorted(set(indexed) | set(_segment_numbers(self.root)))
        for number in numbers:
            path = self.root / "segments" / f"seg-{number:06d}.jsonl"
            if number in indexed and (not path.exists() or not _regular(path)):
                missing_segments.append(number)
                continue
            if not path.exists() or not _regular(path):
                continue
            parsed, bad = _read_jsonl(path)
            loaded.extend(parsed)
            corrupt += bad
        active = self.active_path()
        if active.exists() and _regular(active):
            parsed, bad = _read_jsonl(active)
            loaded.extend(parsed)
            corrupt += bad
        elif active.exists():
            corrupt += 1
        loaded.sort(key=lambda record: record.get("receipt_seq", 0))
        return loaded, missing_segments, corrupt

    def _rotate(self):
        number = self.meta["active_segment"]
        target = self.root / "segments" / f"seg-{number:06d}.jsonl"
        if self.active_path().exists():
            os.replace(self.active_path(), target)
            os.chmod(target, 0o600)
        self.meta.setdefault("segments", []).append(number)
        self.meta["active_segment"] = number + 1
        self._active_count = 0
        self._persist_meta()

    def _persist_meta(self):
        if not self.writable:
            raise PermissionError("read-only diagnostic journal")
        path = self.root / "meta.json"
        temporary = self.root / "meta.json.tmp"
        payload = json.dumps(self.meta, sort_keys=True, ensure_ascii=True) + "\n"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def _count_lines(self, path):
        if not path.exists() or not _regular(path):
            return 0
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())


def _empty_meta(epoch):
    return {
        "schema_version": 2,
        "journal_epoch": epoch,
        "next_receipt_seq": 1,
        "next_local_producer_seq": 1,
        "segments": [],
        "active_segment": 1,
        "idempotency": {},
        "counter_loss": False,
    }


def _regular(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _data_files(root):
    return _segment_numbers(root) or (root / "active.jsonl").exists()


def _segment_numbers(root):
    folder = root / "segments"
    numbers = []
    if not folder.is_dir():
        return numbers
    for path in folder.iterdir():
        name = path.name
        if path.is_symlink() or not name.startswith("seg-") or not name.endswith(".jsonl"):
            continue
        token = name[4:-6]
        if token.isdigit():
            numbers.append(int(token))
    return sorted(numbers)


def _read_jsonl(path):
    records = []
    corrupt = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                corrupt += 1
    return records, corrupt


def _max_journal_epoch(root):
    epoch = 1
    paths = [root / "active.jsonl"]
    for number in _segment_numbers(root):
        paths.append(root / "segments" / f"seg-{number:06d}.jsonl")
    for path in paths:
        if not path.exists() or not _regular(path):
            continue
        records, _ = _read_jsonl(path)
        for record in records:
            value = record.get("journal_epoch")
            if isinstance(value, int) and value > epoch:
                epoch = value
    return epoch


def _recover_idempotency(root):
    found = {}
    paths = [root / "active.jsonl"]
    for number in _segment_numbers(root):
        paths.append(root / "segments" / f"seg-{number:06d}.jsonl")
    for path in paths:
        if not path.exists() or not _regular(path):
            continue
        records, _ = _read_jsonl(path)
        for record in records:
            scope = record.get("idempotency_scope")
            if isinstance(scope, str) and isinstance(record.get("payload_hash"), str):
                found[scope] = {"hash": record["payload_hash"], "record_id": record.get("record_id")}
    return found


def _max_receipt(root):
    highest = 0
    paths = [root / "active.jsonl"]
    for number in _segment_numbers(root):
        paths.append(root / "segments" / f"seg-{number:06d}.jsonl")
    for path in paths:
        if not path.exists() or not _regular(path):
            continue
        records, _ = _read_jsonl(path)
        for record in records:
            value = record.get("receipt_seq")
            if isinstance(value, int) and value > highest:
                highest = value
    return highest
