"""Health evidence for one bound candidate.

Local service identity is the health_ok input. A public-origin observation
is stored beside it and is never treated as the same evidence.
"""


def identity_matches(local, candidate_head, service_id, artifact_digest):
    if not isinstance(local, dict):
        return False
    return (
        local.get("running_sha") == candidate_head
        and local.get("service_id") == service_id
        and local.get("artifact_digest") == artifact_digest
    )


def health_ok(deployment):
    """True only when the local probe matches this candidate, artifact, and service."""
    local = deployment.get("local_health") or {}
    if local.get("ok") is not True:
        return False
    candidate = deployment["candidate"]
    return identity_matches(
        local,
        candidate["head"],
        deployment["service_id"],
        deployment["artifact_digest"],
    )


def misdiagnosed(deployment):
    """The probe claimed success, but the running identity is a different target."""
    local = deployment.get("local_health") or {}
    if local.get("ok") is not True:
        return False
    return not health_ok(deployment)


def no_change_ready(deployment):
    local = deployment.get("local_health") or {}
    previous = deployment.get("previous_healthy_sha")
    candidate = deployment["candidate"]
    return (
        deployment.get("install_kind") == "upgrade"
        and bool(previous)
        and deployment.get("health_checked_before_no_change") is True
        and not deployment.get("pending")
        and local.get("ok") is True
        and local.get("running_sha") == previous == candidate["head"]
        and local.get("service_id") == deployment["service_id"]
        and local.get("artifact_digest") == deployment["artifact_digest"]
        and deployment.get("artifact_digest") == deployment.get("previous_artifact_digest")
    )


def old_health_ready(deployment, artifact_digest_on_disk):
    local = deployment.get("local_health") or {}
    previous = deployment.get("previous_healthy_sha")
    expected = deployment.get("previous_artifact_digest")
    if deployment.get("install_kind") != "upgrade" or not previous or not expected:
        return False
    if artifact_digest_on_disk != expected:
        return False
    return (
        local.get("ok") is True
        and local.get("running_sha") == previous
        and local.get("service_id") == deployment["service_id"]
        and local.get("artifact_digest") == expected
    )
