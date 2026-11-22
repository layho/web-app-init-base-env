# Mac mini shared PR test worker (v1)

This service is independent of production applications. It scans approved repositories once per minute when the user LaunchAgent is loaded, processes one PR at a time, and posts commit status context `macmini/isolated-tests-v1`. It does NOT merge or deploy PRs.

## Install

Prerequisites: macOS, Python 3, Git SSH access to the three approved repositories, and a running local Docker engine (Docker Desktop or Colima). If you already run Docker, retain the existing engine. A fresh Docker setup can use Homebrew's docker, docker-buildx and colima. Set a modest VM CPU/RAM/disk budget; don't change the existing production runtime.

Create a separate fine-grained GitHub token limited to web-app-init-base-env, macmini-office-app-panel, and xiongzhan-ti-hunter, with Pull requests read and Commit statuses read/write. SSH handles code checkout. The token is entered interactively on Mac mini and saved mode 0600 under ~/.local/share/macmini-pr-tests; never send it in chat or pass it into a test container.

Run from this trusted checkout:
```bash
bash deploy/macci/install.sh
```
Immediately test a specific PR:
```bash
python3 ~/.local/share/macmini-pr-tests/runner.py --repo layho/web-app-init-base-env --pr 14 --force
```
Use --force to retry the same head/base after an environment or dependency failure. The scheduled worker skips completed head/base pairs and runs a new test if either SHA changes. Local job logs and worker stdout/stderr are in ~/.local/share/macmini-pr-tests. The launchd label is com.layho.macci. A GUI user login session and a running Docker engine are required for this LaunchAgent; this is not an unattended boot provisioning solution yet.

## Boundaries and supported projects

- Base: PRs targeting preview. Panel and TI Hunter: PRs targeting main. These are configured candidates, not yet end-to-end verified. Clawer/ERP not enabled in v1; add a project-specific profile and lock file first.
- Only same-repository PRs from OWNER/MEMBER/COLLABORATOR; draft PRs eligible. Reject fork PRs and mismatched base/head merge parents.
- Clone the GitHub-generated merge ref into a disposable bare repository, archive only the candidate tree (not host env or production data), install dependencies in a clean Docker build context, then run pytest, Python compilation, and frontend build as nonroot in a network-disabled container. Dependency preparation requires outbound network access and is not an untrusted-code sandbox; use only reviewed repositories. No host volumes, Docker socket, SSH key or GitHub token are mounted into the test container.
- Python/PIP and npm lockfile profile: root requirements.txt, frontend/package.json and frontend/package-lock.json, tests/test_*.py. Database_URL points to temporary SQLite; tests requiring PostgreSQL or external integrations need a dedicated profile. Linux tests do not prove macOS/launchd or actual public deployment.
- The result status records tested base SHA, GitHub-generated merge SHA and stage; before accepting, verify both current head/base against the status and review required tests. No automatic merges or GitHub branch-protection changes.
- Never use docker system prune: it may interfere with other services. Worker only removes its named test container/image; monitor Docker build cache and local log size.
- Keep the runner itself on a separately reviewed trusted checkout. Do not run its installation from an arbitrary PR. Update only while idle.
- Local regression: run `cd deploy/macci && python3 -m unittest -v test_runner`. Covers allowlist, SHA, status deduplication, and Git archive. Mac/Docker/GitHub writeback integration remains unverified until the first real run.
