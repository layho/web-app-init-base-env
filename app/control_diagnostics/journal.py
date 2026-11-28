"""Durable diagnostic journal.

Records survive restart and rotation. A missing segment or a lost counter
is reported. Reads do not append. This journal is not an authority store.

Writers share one flock for idempotency, sequence assignment, append, and
meta replacement. The lock is a cooperating-process lock on this host. A
torn JSON line stays visible as corruption and is not rewritten into a
valid record. This package does not fabricate records to make a sequence
look continuous. Segment numbers may skip. A missing indexed segment is a
gap. flock does not finish a write the kernel interrupted and is not a
storage-level transaction.
"""

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from app.control_diagnostics.redaction import contains_secret, redact_value
from app.control_diagnostics.safe_io import (
    UnsafePath,
    append_bytes,
    list_names,
    lstat_at,
    open_directory,
    open_root,
    read_bounded,
    replace_bytes,
)

_STREAM = re.compile(r"[A-Za-z0-9_.-]{1,40}:diagnostic\Z")
JOURNAL_RESIDUALS = (
    "A torn JSON line stays visible as corruption and is not rewritten into a valid record.",
    "This package does not fabricate records to make a sequence look continuous.",
    "flock serializes cooperating writers that use this journal on this host. It does not complete a write that the kernel interrupted, and it is not a storage-level transaction.",
    "Segment numbers may skip. A missing indexed segment is a gap.",
    "A symlink or hard link in the journal directory is not read or written through.",
)


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
    def __init__(self, root, meta, writable, limits, counter_loss=False, root_fd=None):
        self.root = Path(root)
        self.meta = meta
        self.writable = writable
        self.limits = limits
        self.counter_loss = counter_loss
        self.root_fd = root_fd
        self._active_count = 0

    @classmethod
    def open(cls, root, *, writable, limits=None):
        root = Path(root)
        limits = limits or Limits()
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("unsafe diagnostic root")
        if writable:
            if not root.exists():
                root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            segments = root / "segments"
            if not segments.exists() and not segments.is_symlink():
                segments.mkdir(mode=0o700)
            if segments.is_dir() and not segments.is_symlink():
                os.chmod(segments, 0o700)
        elif not root.is_dir() or root.is_symlink():
            raise ValueError("unsafe diagnostic root")
        root_fd = open_root(root)
        journal = cls(root, _empty_meta(epoch=1), writable, limits, counter_loss=False, root_fd=root_fd)
        try:
            if writable:
                journal._ensure_lock()
                with journal._exclusive():
                    journal._load_writable_locked()
            else:
                journal._load_readonly()
        except Exception:
            os.close(root_fd)
            journal.root_fd = None
            raise
        journal._active_count = journal._count_locked("active.jsonl")
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
            "residuals": list(JOURNAL_RESIDUALS),
            "active_gaps": list(self.meta.get("active_gaps") or []),
        }

    @contextmanager
    def exclusive(self):
        """Reload durable state and hold the cross-process writer lock."""
        if not self.writable:
            raise PermissionError("read-only diagnostic journal")
        with self._exclusive():
            self._reload_locked()
            self._prepare_locked()
            yield self

    def find_idempotency(self, scope):
        return (self.meta.get("idempotency") or {}).get(scope)

    def write_locked(self, record):
        """Append one record. Caller holds exclusive()."""
        stored = redact_value(record)
        encoded = (json.dumps(stored, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        if contains_secret(encoded.decode("ascii")):
            raise ValueError("redaction failed closed")
        if self._active_count >= self.limits.max_records_per_segment:
            self._rotate_locked()
        append_bytes(self.root_fd, "active.jsonl", encoded)
        self._active_count += 1
        self.meta["next_receipt_seq"] = stored["receipt_seq"] + 1
        scope = stored.get("idempotency_scope")
        if scope:
            self.meta.setdefault("idempotency", {})[scope] = {
                "hash": stored["payload_hash"],
                "record_id": stored["record_id"],
            }
        if stored.get("local_seq"):
            self.meta["next_local_producer_seq"] = stored["producer_seq"] + 1
        self._persist_meta_locked()
        return stored

    def append(self, record):
        with self.exclusive():
            return self.write_locked(record)

    def records(self):
        with self._shared():
            return self._records_locked()

    def _ensure_lock(self):
        try:
            fd = os.open(
                "commit.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self.root_fd,
            )
        except OSError as exc:
            raise ValueError("unsafe journal lock") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe journal lock")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    @contextmanager
    def _exclusive(self):
        fd = os.open("commit.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.root_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _shared(self):
        try:
            fd = os.open("commit.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.root_fd)
        except OSError:
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_writable_locked(self):
        state = self._meta_state()
        if state == "symlink":
            raise ValueError("unsafe diagnostic meta")
        if state == "present":
            self.meta = json.loads(self._read_meta_locked().decode("utf-8"))
            self.counter_loss = bool(self.meta.get("counter_loss"))
        elif self._has_data_locked():
            epoch = self._max_journal_epoch_locked() + 1
            self.meta = _empty_meta(epoch=epoch)
            self.meta["counter_loss"] = True
            self.counter_loss = True
            numbers = self._segment_numbers_locked()
            self.meta["segments"] = numbers
            self.meta["active_segment"] = (max(numbers) + 1) if numbers else 1
            self.meta["next_receipt_seq"] = 1
            self.meta["idempotency"] = {}
        else:
            self.meta = _empty_meta(epoch=1)
            self.counter_loss = False
        self._active_count = self._count_locked("active.jsonl")
        changed = self._prepare_locked()
        if state != "present" or changed:
            self._persist_meta_locked()

    def _load_readonly(self):
        state = self._meta_state()
        if state == "symlink":
            raise ValueError("unsafe diagnostic meta")
        if state == "present":
            self.meta = json.loads(self._read_meta_locked().decode("utf-8"))
            self.counter_loss = bool(self.meta.get("counter_loss"))
            self._active_count = self._count_locked("active.jsonl")
            return
        if self._has_data_locked():
            self.meta = _empty_meta(epoch=1)
            self.meta["journal_epoch"] = None
            self.meta["counter_loss"] = True
            self.meta["segments"] = self._segment_numbers_locked()
            self.counter_loss = True
            return
        self.meta = _empty_meta(epoch=1)
        self.counter_loss = False

    def _prepare_locked(self):
        changed = self._adopt_locked()
        if self._reconcile_receipts_locked():
            changed = True
        return changed

    def _reload_locked(self):
        state = self._meta_state()
        if state == "symlink":
            raise ValueError("unsafe diagnostic meta")
        if state == "present":
            self.meta = json.loads(self._read_meta_locked().decode("utf-8"))
        self.counter_loss = bool(self.meta.get("counter_loss"))
        self._active_count = self._count_locked("active.jsonl")

    def _records_locked(self):
        loaded = []
        missing_segments = []
        corrupt = 0
        indexed = [number for number in self.meta.get("segments") or [] if isinstance(number, int)]
        seg_fd = None
        names = set()
        try:
            seg_fd = open_directory(self.root_fd, "segments")
        except (OSError, UnsafePath):
            seg_fd = None
        try:
            if seg_fd is not None:
                for name in list_names(seg_fd):
                    number = _segment_number(name)
                    if number is None:
                        continue
                    if _is_regular(seg_fd, name):
                        names.add(number)
            numbers = sorted(set(indexed) | names)
            for number in numbers:
                name = f"seg-{number:06d}.jsonl"
                if seg_fd is None or number not in names:
                    if number in indexed:
                        missing_segments.append(number)
                    continue
                parsed, bad, refused = _read_jsonl(seg_fd, name, self.limits.max_segment_bytes)
                if refused:
                    if number in indexed:
                        missing_segments.append(number)
                    else:
                        corrupt += 1
                    continue
                loaded.extend(parsed)
                corrupt += bad
            parsed, bad, refused = _read_jsonl(self.root_fd, "active.jsonl", self.limits.max_segment_bytes)
            if refused:
                corrupt += 1
            else:
                loaded.extend(parsed)
                corrupt += bad
        finally:
            if seg_fd is not None:
                os.close(seg_fd)
        loaded.sort(key=lambda record: record.get("receipt_seq") if isinstance(record.get("receipt_seq"), int) else 0)
        return loaded, missing_segments, corrupt

    def _rotate_locked(self):
        seg_fd = open_directory(self.root_fd, "segments")
        try:
            number = self.meta.get("active_segment") or 1
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                number = 1
            while _is_regular(seg_fd, f"seg-{number:06d}.jsonl") or _name_exists(seg_fd, f"seg-{number:06d}.jsonl"):
                number += 1
                if number > 100000:
                    raise UnsafePath("segment space exhausted")
            if _is_regular(self.root_fd, "active.jsonl"):
                os.rename(
                    "active.jsonl",
                    f"seg-{number:06d}.jsonl",
                    src_dir_fd=self.root_fd,
                    dst_dir_fd=seg_fd,
                )
                fd = os.open(
                    f"seg-{number:06d}.jsonl",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=seg_fd,
                )
                try:
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
                os.fsync(seg_fd)
                os.fsync(self.root_fd)
            elif _name_exists(self.root_fd, "active.jsonl"):
                raise UnsafePath("active journal is not a regular file")
            segments = list(self.meta.get("segments") or [])
            if number not in segments:
                segments.append(number)
            self.meta["segments"] = segments
            self.meta["active_segment"] = number + 1
            self._active_count = 0
            self._persist_meta_locked()
        finally:
            os.close(seg_fd)

    def _adopt_locked(self):
        try:
            seg_fd = open_directory(self.root_fd, "segments")
        except (OSError, UnsafePath):
            return False
        try:
            found = []
            for name in list_names(seg_fd):
                number = _segment_number(name)
                if number is not None and _is_regular(seg_fd, name):
                    found.append(number)
        finally:
            os.close(seg_fd)
        changed = False
        current = [number for number in self.meta.get("segments") or [] if isinstance(number, int)]
        union = sorted(set(current) | set(found))
        if union != sorted(current):
            self.meta["segments"] = union
            changed = True
        if found:
            nxt = max(found) + 1
            active = self.meta.get("active_segment") or 1
            if not isinstance(active, int) or isinstance(active, bool) or active <= max(found):
                self.meta["active_segment"] = nxt
                changed = True
        return changed

    def _reconcile_receipts_locked(self):
        records, _, _ = self._records_locked()
        highest = 0
        seen = set()
        disk_items = {}
        conflict = False
        for record in records:
            value = record.get("receipt_seq")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                seen.add(value)
                if value > highest:
                    highest = value
            scope = record.get("idempotency_scope")
            digest = record.get("payload_hash")
            if isinstance(scope, str) and isinstance(digest, str):
                current = disk_items.get(scope)
                if current is None:
                    disk_items[scope] = {"hash": digest, "record_id": record.get("record_id")}
                elif current.get("hash") != digest:
                    disk_items[scope] = {"hash": None, "record_id": None, "conflict": True}
                    conflict = True
        changed = False
        idem = dict(self.meta.get("idempotency") or {})
        for scope, item in disk_items.items():
            current = idem.get(scope)
            if current is None:
                idem[scope] = item
                changed = True
            elif item.get("conflict") or current.get("hash") != item.get("hash"):
                idem[scope] = {"hash": None, "record_id": None, "conflict": True}
                changed = True
                conflict = True
        if idem != (self.meta.get("idempotency") or {}):
            self.meta["idempotency"] = idem
            changed = True
        floor = highest + 1
        next_receipt = self.meta.get("next_receipt_seq")
        if not isinstance(next_receipt, int) or isinstance(next_receipt, bool) or next_receipt < floor:
            self.meta["next_receipt_seq"] = floor
            changed = True
            next_receipt = floor
        hole = bool(seen) and any(number not in seen for number in range(1, highest + 1))
        ahead = isinstance(next_receipt, int) and next_receipt > floor
        if hole or ahead or conflict:
            note = "receipt sequence is not continuous; this package did not fabricate the missing records"
            gaps = list(self.meta.get("active_gaps") or [])
            if note not in gaps:
                gaps.append(note)
                self.meta["active_gaps"] = gaps
                changed = True
            if not self.meta.get("counter_loss"):
                self.meta["counter_loss"] = True
                self.counter_loss = True
                changed = True
        return changed

    def _persist_meta_locked(self):
        if not self.writable:
            raise PermissionError("read-only diagnostic journal")
        payload = (json.dumps(self.meta, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        replace_bytes(self.root_fd, "meta.json", payload)

    def _read_meta_locked(self):
        try:
            return read_bounded(self.root_fd, "meta.json", self.limits.max_segment_bytes)
        except UnsafePath as exc:
            raise ValueError("unsafe diagnostic meta") from exc

    def _meta_state(self):
        try:
            info = lstat_at(self.root_fd, "meta.json")
        except FileNotFoundError:
            return "absent"
        if stat.S_ISLNK(info.st_mode):
            return "symlink"
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("unsafe diagnostic meta")
        return "present"

    def _count_locked(self, name):
        try:
            data = read_bounded(self.root_fd, name, self.limits.max_segment_bytes)
        except FileNotFoundError:
            return 0
        except UnsafePath:
            return 0
        return sum(1 for line in data.splitlines() if line.strip())

    def _has_data_locked(self):
        if _is_regular(self.root_fd, "active.jsonl"):
            return True
        return bool(self._segment_numbers_locked())

    def _segment_numbers_locked(self):
        try:
            seg_fd = open_directory(self.root_fd, "segments")
        except (OSError, UnsafePath):
            return []
        try:
            numbers = []
            for name in list_names(seg_fd):
                number = _segment_number(name)
                if number is not None and _is_regular(seg_fd, name):
                    numbers.append(number)
            return sorted(numbers)
        finally:
            os.close(seg_fd)

    def _max_journal_epoch_locked(self):
        epoch = 1
        records, _, _ = self._records_locked()
        for record in records:
            value = record.get("journal_epoch")
            if isinstance(value, int) and not isinstance(value, bool) and value > epoch:
                epoch = value
        return epoch


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


def _segment_number(name):
    if not isinstance(name, str) or not name.startswith("seg-") or not name.endswith(".jsonl"):
        return None
    token = name[4:-6]
    if not token.isdigit():
        return None
    return int(token)


def _is_regular(dir_fd, name):
    try:
        info = lstat_at(dir_fd, name)
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _name_exists(dir_fd, name):
    try:
        lstat_at(dir_fd, name)
    except (FileNotFoundError, OSError):
        return False
    return True


def _read_jsonl(dir_fd, name, max_bytes):
    try:
        data = read_bounded(dir_fd, name, max_bytes)
    except FileNotFoundError:
        return [], 0, False
    except UnsafePath:
        return [], 0, True
    records = []
    corrupt = 0
    text = data.decode("utf-8", errors="replace")
    if data and not data.endswith(b"\n"):
        corrupt += 1
        text = text.rsplit("\n", 1)[0]
    for line in text.splitlines():
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
    return records, corrupt, False


def commit_fields(record):
    """Validate a sequence number and stream id. Returns an error string or None."""
    seq = record.get("producer_seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return "producer_seq must be a positive integer."
    stream_id = record.get("stream_id")
    if not isinstance(stream_id, str) or _STREAM.fullmatch(stream_id) is None:
        return "stream_id must be a diagnostic stream."
    return None
