# HostOps phase 1 — local diagnostic core

Implements two read-only operations, host.health and service.inventory, with no caller-supplied paths or commands. Run the regression suite in an isolated environment: python3 -m unittest discover -s deploy/hostops -p 'test_*.py'. Local CLI is not a remote agent and is not installed or launched by this PR.

Health reports home-volume capacity; CPU and memory utilization remain explicitly unavailable. Service inventory reports only allowlisted labels visible in the executing user's launchd domain; not_visible does not prove a service is uninstalled. No raw launchctl output or environment is returned. No networking, remote task execution, MacCI credentials, production DB migration, or service mutation. Gateway, signed jobs, separate service identity, UI data binding and actual ChatGPT tool integration remain future implementation and verification steps.

Design reference: PR #188. This implementation is independent of its UI changes to preserve the already tested UI candidate.

The `agent.snapshot` operation emits a combined local heartbeat payload with health, service inventory and supported capabilities. It explicitly reports local-only transport; it does not register with a gateway or send network requests. Disk errors yield null, not fabricated capacity. Malformed service status entries are ignored.

## Outbound heartbeat client (candidate)
`agent.py --once` sends a read-only heartbeat to the configured HTTPS gateway; continuous mode uses 15-second normal cadence and bounded failure backoff. Configure HOSTOPS_HOST_ID, HOSTOPS_GATEWAY_ORIGIN, HOSTOPS_AGENT_TOKEN_FILE locally; the token file must be a regular non-symlink owned by the executing user with mode 0600. No token is accepted on the command line. TLS verification is retained and redirects rejected. No installer, job execution, or real host connectivity is verified by these offline tests. Earlier local-only descriptions refer to diagnostics.py.

## Local installation candidate
The operator runs install.py as a dedicated non-root macOS login user, with independently reviewed release digest (SHA-256 of the compact sorted JSON mapping the three runtime filenames to their SHA-256 hashes). Stage with --source, --digest, --host and --origin; --activate is a separate explicit local action. Before staging, prepare ~/.hostops (0700), agent.token (0600), and optionally job.key (0600) for --dispatch. Keep the gateway token and signing key separate. No credentials are embedded in the plist. Previous verified runtime files remain available. The installer probes HTTPS heartbeat before activation, restores the previous plist on activation failure, and does not alter MacCI or any other service. It registers a user LaunchAgent (login startup, not pre-login boot). Actual launchd, service identity and reboot verification require macOS acceptance. No remote upgrade authority is granted.
