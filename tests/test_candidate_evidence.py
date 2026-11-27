import pytest
from app.candidate_evidence import Candidate, compare_release, check_candidate

H,B,M,T,N=("a"*40,"b"*40,"c"*40,"d"*40,"e"*40)
C=Candidate(H,B,M,T)


def test_release_tree_and_ancestry():
    assert compare_release(M,T,B,T,preview_is_ancestor=True)=="RELEASED_SAME_TREE"
    assert compare_release(M,T,B,T,preview_is_ancestor=False)=="SAME_TREE_UNRELATED"
    assert compare_release(M,T,B,N,preview_is_ancestor=True)=="DIFFERENT_TREE"
    assert compare_release(M,T,M,T,preview_is_ancestor=True)=="IDENTICAL_COMMIT"
    with pytest.raises(ValueError):
        compare_release(M,T,M,N,preview_is_ancestor=True)


def check(**overrides):
    args=dict(current_head=H,current_base=B,current_merge=M,current_tree=T,
              merge_parents=(B,H),trusted_test_candidate=C)
    args.update(overrides)
    return check_candidate(C,**args)


@pytest.mark.parametrize(("overrides","expected"),[
    ({"head_already_integrated":True},"ALREADY_INTEGRATED"),
    ({"current_head":N},"HEAD_CHANGED"),
    ({"current_base":N},"BASE_CHANGED"),
    ({"current_merge":N},"CANDIDATE_CHANGED"),
    ({"current_tree":N},"CANDIDATE_CHANGED"),
    ({"merge_parents":(H,B)},"INVALID_MERGE_PARENTS"),
    ({"trusted_test_candidate":None},"TEST_EVIDENCE_MISSING_OR_STALE"),
    ({"trusted_test_candidate":Candidate(H,N,M,T)},"TEST_EVIDENCE_MISSING_OR_STALE"),
    ({},"EVIDENCE_MATCH"),
])
def test_exact_evidence(overrides,expected):
    assert check(**overrides)==expected
