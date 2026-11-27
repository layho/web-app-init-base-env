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
