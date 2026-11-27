# V0.5.0 integration queue (#153, #138)

Initial implementation: a transactionally serialized SQLite planning ledger for one active version, immutable task identities, dependency order, path reservations, exact base SHA and expiring leases. A lease expiry enters UNKNOWN and blocks automatic dispatch; preparation completion does not imply testing, merge or deployment.

This module is not yet wired to the production coordinator. Its database must be located in an independently protected private state directory. Do not expose leases in browser responses. Only a trusted coordinator may supply actual Git diff paths and verified candidate evidence. The PREPARED -> INTEGRATED transition, evidence verification, reconciliation, authorization, migration, UI and host integration remain to implement. No existing publication gate is changed.

## Recovery and fallback tests
UNKNOWN recovery requires a trusted local fencing verifier bound to the exact job/head/base and expiry event sequence. Evidence identifiers are consumed once per job; a stale lease cannot finish a new attempt. The verifier must establish that no integration side effect occurred. No web endpoint or production fencing adapter is enabled yet. If the outcome cannot be established, retain UNKNOWN.

Fallback CI is triggered only when the repository owner explicitly adds the authorized-fallback-test label to a same-repository preview PR. Remove and reapply the label for a separately authorized run after a new candidate; synchronizations do not trigger a run. Results are not MacCI receipts or merge authorization.
