"""Cold-start chain.

A hostops pass count is not evidence that the boot chain ran. Every link
below has to be true on its own. This module does not start a host service.
"""

from app.control_deploy.contract_machine import digest_of
from app.control_deploy.health import identity_matches


LINKS = (
    "journal_loaded",
    "lock_consistent",
    "artifact_rehashed",
    "service_identity_checked",
    "health_rechecked",
    "migration_not_applied",
    "production_disabled",
)


def _inventory_matches(inventory, deployment):
    head = deployment["candidate"]["head"]
    for item in inventory:
        if (
            item.get("service_id") == deployment["service_id"]
            and item.get("running_sha") == head
            and item.get("artifact_digest") == deployment["artifact_digest"]
        ):
            return True
    return False


def evaluate_boot(
    *,
    deployment,
    journal_seq,
    lock,
    artifact_body,
    service_local,
    inventory,
    migration_executed,
    production_enablement,
    hostops_pass_count,
):
    links = {name: False for name in LINKS}
    links["journal_loaded"] = bool(deployment) and journal_seq > 0
    links["production_disabled"] = production_enablement is False
    links["migration_not_applied"] = migration_executed is False
    if deployment and deployment.get("state") == "COMMIT":
        links["lock_consistent"] = lock is None and _inventory_matches(inventory, deployment)
        disk_digest = digest_of(artifact_body) if isinstance(artifact_body, bytes) else None
        links["artifact_rehashed"] = disk_digest == deployment.get("artifact_digest")
        links["service_identity_checked"] = identity_matches(
            service_local,
            deployment["candidate"]["head"],
            deployment["service_id"],
            deployment["artifact_digest"],
        )
        links["health_rechecked"] = (
            isinstance(service_local, dict)
            and service_local.get("ok") is True
            and links["service_identity_checked"]
        )
    elif deployment:
        links["lock_consistent"] = bool(
            lock
            and lock.get("generation") == deployment.get("fence_generation")
            and lock.get("run_id") == deployment.get("owner_run_id")
            and lock.get("deployment_id") == deployment.get("deployment_id")
        )
    proved = all(links.values())
    return {
        "boot_recovery_proved": proved,
        "links": links,
        "hostops_pass_count_ignored": hostops_pass_count,
        "production_enablement": False,
        "migration_executed": False,
    }
