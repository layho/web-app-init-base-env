"""Three-way text classification. No result is written back to a tree."""

import difflib


def _spans(left, right):
    spans = []
    matcher = difflib.SequenceMatcher(a=left, b=right)
    for tag, start, end, _other_start, _other_end in matcher.get_opcodes():
        if tag != "equal":
            spans.append((start, end))
    return spans


def _overlaps(left, right):
    for first_start, first_end in left:
        for second_start, second_end in right:
            if first_start == first_end == second_start == second_end:
                return True
            if first_start < second_end and second_start < first_end:
                return True
    return False


def classify_text(old, new, local):
    """Return unchanged, foundation_only, text_separable, or ambiguous.

    Separable edits are still not merged. Ambiguous edits stay unresolved.
    """

    values = (old, new, local)
    if any(not isinstance(value, str) for value in values):
        return "ambiguous"
    if any("\0" in value for value in values):
        return "ambiguous"
    if old == new or local == new:
        return "unchanged"
    if local == old:
        return "foundation_only"
    old_lines = old.splitlines()
    if _overlaps(_spans(old_lines, new.splitlines()), _spans(old_lines, local.splitlines())):
        return "ambiguous"
    return "text_separable"
