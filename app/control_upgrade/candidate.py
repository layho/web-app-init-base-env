"""W00 candidate encoding. The id is not evidence that a build or deploy ran."""

import hashlib


def candidate_id(repo, task, head, base, merge, tree):
    raw = "\n".join(["v1", repo, task, head, base, merge, tree]).encode("utf-8")
    return "cand_" + hashlib.sha256(raw).hexdigest()
