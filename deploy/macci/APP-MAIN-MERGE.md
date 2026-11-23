# Application main merge worker — stage 1 (Issue #86)

This is a **new, host-only coordinator**, intentionally separate from the
foundation preview merge worker and from the foundation's main publisher.

Scope: only entries in the fixed MacCI Runner allowlist with a **main-only**
branch. The base repository is excluded. The program accepts only a fixed
\`--repo\` choice, never a GitHub URL, arbitrary shell command, target branch,
token, or HTTP payload. It reuses the existing protected MacCI token file.
Nothing is installed or enabled by merging this source PR.

## Release gate

For each application PR: the PR must be open, **not Draft**, from the same
allowed repository, owner-authored, mergeable and clean, targeting \`main\`.
The newest exact-head OWNER comment must be:

\`\`\`text
[APP_MAIN_REVIEW] {"schema":1,"head":"<40 hex SHA>","decision":"approved","base":"<40 hex current main SHA>"}
\`\`\`

A later OWNER decision such as \`revoked\`, or a changed HEAD/main, invalidates
the approval. The exact current GitHub synthetic merge must have ordered
parents \`[main, HEAD]\` and a valid tree. The **latest** MacCI status for HEAD
must be OWNER-authored \`success\` with description exactly
\`base=<main> merge=<synthetic merge SHA> passed\`. The coordinator fetches
the immutable PR merge candidate, rechecks its ordered parents/tree, and
rechecks the authorization before a **non-force push** to the same app's main.
It will not auto-release Draft PRs (the owner must complete a separate review).

## Safe developer verification

From a reviewed source checkout:

\`\`\`sh
cd deploy/macci
python3 -m unittest -v test_merge_application
python3 merge_application.py --repo layho/macmini-office-app-panel --dry-run
\`\`\`

The dry run still requires protected host token and Git SSH access. **Do not**
enable a recurring host job before all tests, operator review and production
deployment/rollback processes have been verified for that app. This stage
does not automatically register launchd, trigger downstream deployment, or
prove application runtime SHA. Its presence alone is not a closed release
loop. A separate later PR must add controlled installation, application
deployment receipts, rollback/freeze, and unified user-facing manual trigger.

No changes to deployed applications, production databases, Cloudflare
Tunnel, credentials, or GitHub Actions are made by this PR.
