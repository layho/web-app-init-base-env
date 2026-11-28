"""Base-platform version identification. No network and no scheduler enrollment."""

import re

from app.control_upgrade.flags import CACHE_TTL_SECONDS, FOUNDATION_REPOSITORY

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


def valid_sha(value):
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def identify_declared(declared):
    if not isinstance(declared, dict):
        return None
    if declared.get("schema") != 1 or declared.get("repository") != FOUNDATION_REPOSITORY:
        return None
    version = declared.get("version")
    sha = declared.get("sha")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        return None
    if not valid_sha(sha):
        return None
    return {"version": version, "sha": sha}


def identify_scheduler(report):
    blocked = report.get("status") == "BLOCKED" or report.get("evidence_state") != "present"
    sha = report.get("sha")
    identified = (
        not blocked
        and report.get("present") is True
        and report.get("symlink") is not True
        and report.get("owner_matches") is True
        and report.get("mode_exclusive") is True
        and valid_sha(sha)
    )
    return {
        "sha": sha if identified else None,
        "identified": identified,
        "enrolled": False,
        "state": "present" if identified else "unknown" if blocked else "unidentified",
        "source": "port",
        "stale": False,
    }


def repository_view(identified):
    if identified is None:
        return {"sha": None, "version": None, "state": "unidentified", "source": "missing", "stale": False}
    return {
        "sha": identified["sha"],
        "version": identified["version"],
        "state": "identified",
        "source": "request",
        "stale": False,
    }


def sha_view(value, *, key="sha"):
    if not valid_sha(value):
        body = {key: None, "state": "unknown", "source": "missing", "stale": False}
        return body
    return {key: value, "state": "present", "source": "port", "stale": False}


def cache_is_stale(age_seconds):
    return age_seconds >= CACHE_TTL_SECONDS
