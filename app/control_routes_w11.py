"""Joint control board. Reads and manual actions use the server grant.

The session is the browser gate. The grant file and AuthzService decide which
tasks can be read or acted on. An empty list is the authorized scope with no
rows, not a constant placeholder.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import authorize_session
from app.control_joint.surface import read_board, submit_action
from app.db import get_db

router = APIRouter()


@router.get("/api/v1/control/board")
def control_board(request: Request, db: Session = Depends(get_db)):
    username = authorize_session(request, db)
    body = read_board(username)
    result = JSONResponse(body, status_code=body.get("http", 200))
    result.headers["Cache-Control"] = "no-store"
    return result


@router.post("/api/v1/control/board/actions")
async def control_action(request: Request, db: Session = Depends(get_db)):
    username = authorize_session(request, db)
    try:
        document = await request.json()
    except Exception:
        document = None
    if not isinstance(document, dict):
        document = {}
    body = submit_action(username, document)
    result = JSONResponse(body, status_code=body.get("http", 200))
    result.headers["Cache-Control"] = "no-store"
    return result
