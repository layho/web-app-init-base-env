# Application main merge worker — stage 1 (Issue #86)

This is a **new, host-only coordinator**, intentionally separate from the
foundation preview merge worker and from the foundation's main publisher.

Scope: only repos in the **explicit publish opt-in** set *and* the trusted
MacCI Runner allowlist with a **main-only** branch. For this first rollout,
only `layho/macmini-office-app-panel` is opted in. TI Hunter is legacy and
**not** opted in, even though its PRs may be tested by MacCI. New apps must
undergo a separate reviewed opt-in change; the base repo is always excluded. The program accepts only a fixed
\`--repo\` choice, never a GitHub URL, arbitrary shell command, target branch,
token, or HTTP payload. It reuses the existing protected MacCI token file.
Nothing is installed or enabled by merging this source PR. The exact-PR\nCLI mode shares the identical merge gate with the ordinary scan; it does not\nconstitute a web-exposed manual button or authorize draft transition.

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
python3 merge_application.py --repo layho/macmini-office-app-panel --pr 7 --dry-run
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


## Stage 2: reviewed Mac mini installation

The source includes a separate fail-closed installer, but **merging this
documentation or code never installs a background agent**. Only the Mac mini
operator may carry out the steps below after this PR has itself completed
MacCI review, preview deployment, explicit website acceptance, and main
publication.

On the Mac mini, from the *clean, exact accepted main checkout*:

```sh
cd ~/web-app-init-base-env-preview
git status --porcelain
git checkout main
git pull --ff-only
cd deploy/macci
python3 install-application-main.py --sha <accepted-main-commit> --check
python3 install-application-main.py --sha <accepted-main-commit> --stage
# Do not enable until the protected app-main review procedure is complete.
python3 install-application-main.py --sha <accepted-main-commit> --enable
```

Use the actual installation directory if it differs. The SHA argument must be
the **40-character exact commit** from GitHub main, never the branch label.

- \`--check\`: validates the unchanged checkout, existing private host
  installation, token file and offline coordinator regression suite.
- \`--stage\`: atomically copies only the new \`merge_application.py\` into the
  trusted host directory. Does **not** register launchd or start a merge.
  Refuses to overwrite a different existing installed copy.
- \`--enable\`: requires the staged bytes to match accepted main and creates a
  fixed app-only LaunchAgent \`com.layho.macci-app-main-panel\` with a 60-second
  scan; refuses any preexisting plist rather than overwriting it. Application
  PRs still must be non-Draft, OWNER-approved for their exact HEAD/base and
  have the latest exact MacCI success to merge.

The deployment check remains separate. After an application PR merges, verify
the application updater, \`/api/version\` running SHA, login/session and
deployment receipt before claiming the application is delivered. This stage
does not install an application updater or create a web-side action.

To stop an already enabled coordinator, unload only the explicitly installed
label through normal macOS service administration; never delete global MacCI
tokens, locks, another application's LaunchAgent or production database.


## Merge evidence vs. deployment evidence

When the coordinator confirms the expected immutable candidate is now the
GitHub application's \`main\` SHA, it writes a private atomic receipt under
\`~/.local/share/macmini-pr-tests/app-main-receipts/\`.
The receipt contains the allowlisted repository, PR number, HEAD, previous
main, merged main, immutable tree, timestamp, and:

- \`merge_state = github_main_verified\`
- \`deployment_state = unverified\`

This **never** marks the web application deployed or accepted. A separate
updater must later provide deployment evidence and an actual running SHA,
and the UI must compare those values. Receipts are restricted to the host
service account, created without replacing an existing record, and only
generated after GitHub confirms main equals the exact tested candidate.
No receipt is generated by \`--dry-run\` or a failed/blocked merge.

The host upgrade process must install the reviewed coordinator script before
this receipt feature can execute. A source PR alone does not change the
already-running host worker.


## Recovery after a confirmed merge but failed receipt write

The application merge can already be committed on GitHub when the host loses
power or the local receipt write fails. Closed PRs are not revisited by the
normal open-PR scan. A trusted Mac mini operator may recover **only the
missing receipt**, without re-merging, using:

```sh
python3 merge_application.py --repo layho/macmini-office-app-panel \
  --reconcile-pr <actual-merged-pr-number> --dry-run
python3 merge_application.py --repo layho/macmini-office-app-panel \
  --reconcile-pr <actual-merged-pr-number>
```

This is neither a deploy operation nor an acceptance override. It requires
the closed PR to be GitHub-confirmed merged, owner-authored, same repository
and main target; the immutable candidate must have the reviewed exact
HEAD/previous-main parents, correct tree, newest owner authorization and
exact MacCI success. It fetches real current main and checks the candidate
is an ancestor, then verifies main and PR state again before writing. A dry
run never writes. An existing receipt is not overwritten. The resulting
deployment status remains **unverified**.

Never use this command for open PRs, failed tests, manually merged
non-reviewed changes, other repositories, or synthetic candidates that
are not in main ancestry.
