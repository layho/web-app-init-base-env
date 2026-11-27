"""Pure checks for trusted Git metadata; never grants release authorization."""
from dataclasses import dataclass
import re


def _sha(value):
    if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{40}",value):
        raise ValueError("invalid Git SHA")
    return value


@dataclass(frozen=True)
class Candidate:
    head: str
    base: str
    merge: str
    tree: str

    def __post_init__(self):
        for value in (self.head,self.base,self.merge,self.tree):
            _sha(value)


def compare_release(main, main_tree, preview, preview_tree, *, preview_is_ancestor):
    """Tree equality describes content only, never an approval or receipt."""
    for value in (main,main_tree,preview,preview_tree):
        _sha(value)
    if type(preview_is_ancestor) is not bool:
        raise ValueError("ancestry must be verified")
    if main==preview:
        if main_tree!=preview_tree or not preview_is_ancestor:
            raise ValueError("inconsistent Git metadata")
        return "IDENTICAL_COMMIT"
    if main_tree==preview_tree:
        return "RELEASED_SAME_TREE" if preview_is_ancestor else "SAME_TREE_UNRELATED"
    return "DIFFERENT_TREE"


def check_candidate(candidate, *, current_head, current_base, current_merge,
                    current_tree, merge_parents, trusted_test_candidate=None,
                    head_already_integrated=False):
    """Fail closed on stale evidence and replacement candidates.

    All metadata, including ancestry and test identity, must come from the
    trusted coordinator, not a request body or PR description.
    A matching receipt remains insufficient for merge authorization.
    """
    if not isinstance(candidate,Candidate):
        raise ValueError("candidate required")
    for value in (current_head,current_base,current_merge,current_tree):
        _sha(value)
    if type(head_already_integrated) is not bool:
        raise ValueError("ancestry must be verified")
    if head_already_integrated:
        return "ALREADY_INTEGRATED"
    if candidate.head!=current_head:
        return "HEAD_CHANGED"
    if candidate.base!=current_base:
        return "BASE_CHANGED"
    if candidate.merge!=current_merge or candidate.tree!=current_tree:
        return "CANDIDATE_CHANGED"
    if not isinstance(merge_parents,(list,tuple)) or tuple(merge_parents)!=(current_base,current_head):
        return "INVALID_MERGE_PARENTS"
    if trusted_test_candidate!=candidate:
        return "TEST_EVIDENCE_MISSING_OR_STALE"
    return "EVIDENCE_MATCH"
