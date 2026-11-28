"""Read-only joint board status. It does not load demo rows or mutate a host."""

from fastapi import APIRouter, Response

from app.control_joint.capability import host_capability

router = APIRouter()


@router.get("/api/v1/control/board")
def control_board(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": 2,
        "side_effect": False,
        "production_enabled": False,
        "production_wired": False,
        "preview_merge_authorized": False,
        "main_release_authorized": False,
        "state": "not_configured",
        "reason": "No control root is configured. This read does not load demo rows, write a journal, or touch a host.",
        "owner": None,
        "recorded_at": None,
        "items": [],
        "history": [],
        "failures": [],
        "macci": host_capability(),
    }
