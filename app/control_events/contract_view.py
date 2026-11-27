"""Read-only view of the frozen W00 contract. This module never writes it."""

from functools import lru_cache
import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "v060" / "w00" / "contract.json"


@lru_cache(maxsize=1)
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def event_spec() -> dict:
    return contract()["entities"]["event"]
