# HostOps phase 1 — local diagnostic core

Implements two read-only operations, host.health and service.inventory, with no caller-supplied paths or commands. Run the regression suite in an isolated environment: python3 -m unittest discover -s deploy/hostops -p 'test_*.py'. Local CLI is not a remote agent and is not installed or launched by this PR.

Health reports home-volume capacity; CPU and memory utilization remain explicitly unavailable. Service inventory reports only allowlisted labels visible in the executing user's launchd domain; not_visible does not prove a service is uninstalled. No raw launchctl output or environment is returned. No networking, remote task execution, MacCI credentials, production DB migration, or service mutation. Gateway, signed jobs, separate service identity, UI data binding and actual ChatGPT tool integration remain future implementation and verification steps.

Design reference: PR #188. This implementation is independent of its UI changes to preserve the already tested UI candidate.
