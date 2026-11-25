import asyncio

import httpx
import pytest

from app import request_limits
from app.main import app


@pytest.mark.parametrize("path", ["/api/v1/feedback", "/api/v1/releases/reject"])
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}, {"Content-Length": "100"}])
def test_oversize_rejected_before_json_parsing(monkeypatch, path, headers):
    monkeypatch.setattr(request_limits, "SCREENSHOT_LIMIT", 8)
    async def chunks():
        yield b"broken "
        yield b"json"

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post(path, content=chunks(), headers=headers)
        assert response.status_code == 413

    asyncio.run(scenario())


def test_overlapping_uploads_release_slots():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0

        async def downstream(scope, receive, send):
            nonlocal active
            active += 1
            if active == request_limits.MAX_SCREENSHOT_REQUESTS:
                entered.set()
            await release.wait()

        limiter = request_limits.RequestBodyLimits(downstream)
        scope = {"type": "http", "method": "POST", "path": "/api/v1/feedback", "headers": []}
        sent = []

        async def send(message):
            sent.append(message)

        tasks = [
            asyncio.create_task(limiter(scope, None, send))
            for _ in range(request_limits.MAX_SCREENSHOT_REQUESTS)
        ]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await limiter(scope, None, send)
            assert sent[0]["status"] == 413
        finally:
            release.set()
            await asyncio.gather(*tasks)
        await limiter(scope, None, send)
        assert active == request_limits.MAX_SCREENSHOT_REQUESTS + 1

    asyncio.run(scenario())
