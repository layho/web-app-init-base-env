"""Load the W00 deployment machine. This module does not define a second one."""

import hashlib
import json
from pathlib import Path


DEFAULT_CONTRACT = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "v060" / "w00" / "contract.json"
)

RECONCILE_SOURCES = ("STAGE", "VERIFY", "SWITCH", "HEALTH", "COMMIT_PENDING")
TERMINAL = ("COMMIT", "ROLLED_BACK", "SAFE_FROZEN")


class ContractError(RuntimeError):
    pass


def load_contract(path=None):
    contract_path = Path(path) if path else DEFAULT_CONTRACT
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    if data["identity"]["production_verification_enabled"] is not False:
        raise ContractError("refusing a contract that enables production verification")
    if data["entities"]["deployment"]["terminal"] != list(TERMINAL):
        raise ContractError("deployment terminal states drifted from COMMIT, ROLLED_BACK, SAFE_FROZEN")
    edges = data["entities"]["deployment"]["transitions"]
    if any(edge["from"] == "SAFE_FROZEN" for edge in edges):
        raise ContractError("SAFE_FROZEN must have no outgoing edge")
    return data, edges, data["entities"]["candidate"]["id"]


def allows(edges, src, dst, guard):
    matched = False
    for edge in edges:
        if edge["from"] == src and edge["to"] == dst:
            matched = True
            edge_guard = edge.get("guard")
            if guard is None:
                if edge_guard is None:
                    return True
            elif edge_guard == guard:
                return True
    return False if matched else False


def candidate_id(spec, repo_id, task_id, head, base, merge, tree):
    if spec.get("trailing_newline"):
        raise ContractError("candidate encoding must not add a trailing newline")
    if spec.get("prefix") != "cand_" or spec.get("algorithm") != "sha256":
        raise ContractError("candidate id algorithm drifted")
    raw = "\n".join(["v1", repo_id, task_id, head, base, merge, tree]).encode(spec["encoding"])
    return spec["prefix"] + hashlib.sha256(raw).hexdigest()


def is_sha(value):
    return isinstance(value, str) and len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def digest_of(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def is_digest(value):
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    hexpart = value[7:]
    return len(hexpart) == 64 and all(char in "0123456789abcdef" for char in hexpart)
