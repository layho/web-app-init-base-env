# V0.5.0 integration queue (#153, #138)

Initial implementation: a transactionally serialized SQLite planning ledger for one active version, immutable task identities, dependency order, path reservations, exact base SHA and expiring leases. A lease expiry enters UNKNOWN and blocks automatic dispatch; preparation completion does not imply testing, merge or deployment.

This module is not yet wired to the production coordinator. Its database must be located in an independently protected private state directory. Do not expose leases in browser responses. Only a trusted coordinator may supply actual Git diff paths and verified candidate evidence. The PREPARED -> INTEGRATED transition, evidence verification, reconciliation, authorization, migration, UI and host integration remain to implement. No existing publication gate is changed.

## Recovery and fallback tests
UNKNOWN recovery requires a trusted local fencing verifier bound to the exact job/head/base and expiry event sequence. Evidence identifiers are consumed once per job; a stale lease cannot finish a new attempt. The verifier must establish that no integration side effect occurred. No web endpoint or production fencing adapter is enabled yet. If the outcome cannot be established, retain UNKNOWN.

Fallback CI is triggered only when the repository owner explicitly adds the authorized-fallback-test label to a same-repository preview PR. Remove and reapply the label for a separately authorized run after a new candidate; synchronizations do not trigger a run. Results are not MacCI receipts or merge authorization.


## Candidate evidence
The candidate_evidence module distinguishes commit identity, tree identity and verified ancestry. It rejects stale head/base/merge/tree, mismatched merge parents and stale test identity. Matching evidence is not a publication grant. The trusted coordinator still needs to fetch the actual Git metadata and independently authenticate the receipt before using these checks. No production integration or automatic merge is enabled by this module.


## Read-only diagnostics
The existing system-log API now includes bounded integration queue diagnostics for authenticated browser sessions. Configure APP_INTEGRATION_QUEUE_FILE only with an existing private ledger owned by the service user. The reader uses SQLite read-only mode and never creates a database or changes permissions. It returns no lease, file reservations, evidence IDs or arbitrary log content. Missing configuration, unavailable and uninitialized sources are distinct. This does not provision a production queue or grant the web process cross-user access; a separately protected export mechanism is still needed if the coordinator runs under a different UID.


## GitHub transport recovery
Read-only REST requests retry transient network and invalid-response errors with the same bounded delay as transient HTTP 5xx. POST is never replayed. Successful full Issue/PR collection replaces stale endpoint errors and records the observation time; failed collection retains the prior combined snapshot. Diagnostic probes record individual check times. No credential or raw transport error is exposed.


## Existing MacCI coordinator integration
The installed preview coordinator optionally journals its already-authorized exact SHA push when MACCI_INTEGRATION_JOURNAL=1 is provisioned locally. The original lock, review, exact test receipt, freeze, candidate verification and non-force push are unchanged. The journal is persisted before push; only an independently observed exact remote preview SHA confirms integration. Lost responses remain pending across process restarts; unchanged or different remote SHA blocks further pushes rather than retrying blindly. The new installer copies the journal module, but existing host installation/activation, independent queue reconciliation adapter, operator resolution for ambiguous outcomes and read-only export are still required. No claim of live host activation is made.


Managed mode (`MACCI_MANAGED_QUEUE=1`) uses the same private integration-queue.sqlite3 ledger for planning and preview push receipts. Only a unique PREPARED reservation `pr-N` matching exact head/base and actual fetched changed paths can enter the existing authorized push path. The journal atomically changes it to UNKNOWN before push and INTEGRATED on exact remote observation, including restart reconciliation. Unregistered candidates fail closed. This mode requires trusted host installation and a provisioned active queue; automated registration/claim and ambiguous-outcome operator recovery remain separate follow-up work. No production activation is implied.


The managed coordinator now stages the verified candidate automatically, within a single transaction, from the actual fetched diff after the original authorization gates. It does not overwrite existing reservations or bypass outstanding dependencies, and does not initialize/activate versions from untrusted PR metadata. An operator-provisioned active ledger is still required. Staging has no remote side effect and can be repeated for the identical candidate after a restart.


Definitive single-ref porcelain rejection plus a fresh observation of the original preview base can close a pending attempt as REJECTED and restore its reservation to PREPARED. Subsequent attempts must pass all original gates again. Timeouts, lost responses, unexpected refs and ambiguous remote errors remain pending; no blind retry or force push is allowed.


Host preview push receipts are independently readable through the authenticated system-log API and UI. Configure APP_PREVIEW_JOURNAL_FILE to the existing private same-user journal (or managed ledger). The UI displays exact head/base/candidate, state, timestamps, filtering and details, with explicit unavailable and pending states. It never treats integration as deployment or release. Cross-UID transport, automatic configuration and host runtime provisioning still require integration work; no production access is granted by this reader.


A stale PREPARED reservation can be re-prepared at a freshly verified base when no pending push exists. Known pre-push reservation conflicts defer that candidate rather than starving all following PRs; a pending remote push still blocks the entire coordinator. Missing Draft metadata fails closed. Tests cover the stale-base transition and pending distinction.


The managed coordinator now independently checks a retained PREPARED PR against GitHub. A closed, explicitly unmerged PR can release its exact reservation without touching a remote branch. Any pending push, missing identity, merged PR, Draft or amended candidate remains protected. The cancellation event is visible in the existing diagnostics.


Optional cross-process evidence transport reuses the authenticated HostOps heartbeat. After the gateway is updated, a trusted local operator may enable HOSTOPS_PREVIEW_EVIDENCE=true on the reviewed agent; the agent reads only its fixed private MacCI journal, optionally selecting the managed ledger with MACCI_MANAGED_QUEUE=1. Reports are bounded to 20 records, gateway-validated, persisted through existing snapshot storage and suppressed as stale after 90 seconds. No new command, token, file path input or release authority is exposed. Update gateway before enabling the new agent field; older gateways reject extra fields. This path does not grant access across OS users; agent must already own the journal. No live activation is implied.


Managed-mode bootstrap: a trusted local launch configuration must set MACCI_MANAGED_QUEUE=1, MACCI_ACTIVE_VERSION=0.5.0 and MACCI_ACTIVE_PR=196 (example only). The coordinator creates its private ledger schema and initial active version automatically under the host lock. Only the explicitly configured PR is considered for push, still subject to every original gate. Changing the configured version does not switch an existing ledger, even after jobs integrate; a separately reviewed transition tied to formal publication/acceptance is required. No PR title, label, comment or web request can initialize or switch a version. Dry run does not create a ledger. Existing host launch configuration remains unchanged until explicit trusted activation.
