# Independent foundation upgrade scheduler (macOS)

Only install from an exact accepted official main commit, after verifying MacCI tests. Existing MacCI, preview publishing, application updater, tokens and data are not replaced. Uses existing SSH credentials for Git and a dedicated GitHub REST API token for PR operations; no gh login, device code or sudo. Select only the downstream repositories needed, with Pull requests read/write (plus automatic Metadata read). The installer accepts the token through a hidden interactive prompt and stores it in a private owner-only file (0600); it never writes the token into a plist, Git repository or log. Existing token files are reused, not overwritten. The GUI launchd domain already used by MacCI can be addressed from SSH.

Run from the accepted foundation checkout:
```sh
bash deploy/app-init/install-upgrade-scheduler.sh --sha "$(git rev-parse HEAD)" --check-repo OWNER/APP
bash deploy/app-init/install-upgrade-scheduler.sh --sha "$(git rev-parse HEAD)" --check-repo OWNER/APP --apply
```
Use the same Python 3.10+ interpreter selected by the installer when registering applications (or the interpreter recorded in its LaunchAgent ProgramArguments). Do not default back to macOS system python3.

Register explicitly:
```sh
S="$HOME/.local/share/macmini-foundation-upgrades/runtime/upgrade_scheduler.py"
PY="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$HOME/Library/LaunchAgents/com.layho.foundation-upgrades.plist")"
"$PY" "$S" register --repo layho/macmini-office-app-panel --mode create
"$PY" "$S" register --repo layho/xiongzhan-ti-hunter --mode observe
"$PY" "$S" register --repo layho/xiongzhan-clawer --mode observe
"$PY" "$S" list
"$PY" "$S" tick
```
Panel create mode requires merged, valid provenance and a valid dedicated GitHub PR-write token. Legacy apps without a valid reproducible old source remain observe until their own migration is reviewed and merged. Never invent a base SHA. Registered apps created later by app-init are enrolled automatically only if the scheduler is already installed. Existing manual registrations are not overwritten when rerunning initialization. One job, five-minute interval, no concurrent runs, one open upgrade PR per app. All PRs are draft; testing, review and application deployment remain separate. Failed network calls retry. Conflicts are held until actual app or official main SHA changes (or explicit tick --force). State lives in private ~/.local/share/macmini-foundation-upgrades; logs contain only sanitized outcomes and SHA/PR references. No force push, application main write, live database access, or runtime code hot reload. Check launchctl print gui/$(id -u)/com.layho.foundation-upgrades and scheduler.log for evidence.

## Re-enrollment from app-init

After successful installation or re-verification, `app-init` registers the app in an already installed scheduler using `--mode create --if-absent`. Existing manual registrations (including `observe`) are never overwritten. When the scheduler is missing or registration fails, deployment remains independent and prints a warning. An installed scheduler and active launchd job must be verified separately. Creating a draft PR does not bypass testing, review or release gates.


## Panel trusted automatic upgrade (opt-in pilot)

For the explicitly authorized `layho/macmini-office-app-panel`, a registered `create` scheduler also examines its pending `base-upgrade/<official-main-SHA>` PR. It **does not** auto-merge non-Panel repositories. To promote, the scheduler must verify all of the following at execution time: OWNER-created PR in the app repository, exact current main base and official foundation SHA, matching and safe three-way planned file set (no conflict or manual review), latest trusted `macmini/isolated-tests-v1` success for that exact head/base/merge, and unchanged PR and refs after rechecking. It then marks the draft ready and merges using the expected head SHA. A missing token scope, failed MacCI, stale ref, unexpected file, blocked plan, or API error leaves the draft for manual review. No GitHub Actions or direct application data access is used.

The Panel web deployment remains owned by its existing Mac mini five-minute updater, independent from the upgrade scheduler. GitHub merge alone is **not** proof of a successful rollout; verify application main, running SHA, and website before claiming completion.

### Existing host scheduler must be upgraded separately

After the scheduler changes have passed MacCI, preview acceptance and **official main** publication, use a clean exact-main checkout and the existing dedicated admin account, without sudo:

```bash
SHA="$(git rev-parse HEAD)"
./deploy/app-init/install-upgrade-scheduler.sh --sha "$SHA" --check-repo layho/macmini-office-app-panel
./deploy/app-init/install-upgrade-scheduler.sh --sha "$SHA" --check-repo layho/macmini-office-app-panel --apply --upgrade
```

The upgrade checks SSH and REST access and runs the regression tests, preserves LaunchAgent, token and registry, takes an owner-only backup, and replaces runtime modules while holding the scheduler tick lock. If runtime or launchd configuration is unsafe or altered, the upgrade stops instead of overwriting it. Verify the active on-disk scheduler modules and next read-only status before treating it as active. GitHub merges do not deploy this standalone host runtime automatically.


## Automatic Panel upgrade queue recovery

The approved Panel scheduler handles **one pending foundation-upgrade PR at a time**. A previously generated upgrade need not match the newest official foundation main: the scheduler verifies that the PR's pinned target is either that official SHA or a verified immutable ancestor (GitHub compare merge-base). It then requires the PR's original generator parent, unchanged safe file scope, OWNER author, exact latest MacCI success for the current Panel main and PR head, and fresh unmodified GitHub refs before changing Draft state or merging. On success, the next 5-minute tick plans a new PR for the latest official foundation.

If MacCI fails/is missing, app main moves, the PR has been manually amended, the target diverged, multiple pending PRs exist, or GitHub is unavailable, the scheduler **does not** merge or deploy. It records a non-success status with the PR URL. No token, raw stderr or sensitive business data is logged. A special-case PR requiring review cannot be auto-merged without new verifiable evidence.

The installed Mac mini scheduler uses a **copied runtime**, independent of the GitHub repository. After a reviewed foundation main release, the first host upgrade must be performed using the documented safe installer check and `--apply --upgrade`; do not claim a GitHub merge alone activates these changes. The existing application updater then handles ordinary Panel deployment after any successful PR merge, and actual running SHA still needs to be verified.


## v0.1.31 automatic safe conflict recovery

The planner will automatically resolve \`deploy/macmini/manage.sh\` only when
the old and new generated scripts both exist, a clean three-way merge succeeds,
the output passes \`bash -n\` **without being executed**, and the application's
original \`LABEL_PREFIX\`, \`PORT\`, \`TRACK_BRANCH\`, and \`EXPECTED_ORIGIN\`
remain unchanged. This is a **strict allowlist**, not an override of all
deployment scripts. SQL migrations, other host scripts, deletions, concurrent
overlapping edits, modified application identity, or invalid shell syntax
remain blocked for review.

A \`blocked\` result is re-evaluated on subsequent scheduler ticks instead of
remaining cached forever. This allows newly reviewed resolution rules to take
effect even when the official and app Git SHAs have not changed. It does not
bypass exact source provenance or commit/MacCI/OWNER gates.

The scheduler is a **separately installed Mac mini runtime**; merging v0.1.31
into the foundation's GitHub main does not update its already-running copy.
Use the existing \`--check-repo ... --apply --upgrade\` process above only
after official foundation main acceptance. Verify the installed runtime and
Panel's newest \`checked_at\`, \`blocked_files\`, \`pr_url\`, and target SHA.
The Panel website should eventually show a real \`base-upgrade/<official-main-SHA>\`
draft PR if and only if **all** planned changes can be resolved safely.

A blocked file list can contain legitimate, app-specific overlap unrelated to
the host script. For these, inspect the real file-specific diff and write a
reviewed, narrow resolver; never add a generic "accept theirs", "accept ours",
or partial provenance update to disguise incomplete upgrades.


## v0.1.32: exact provenance recovery for five Panel false conflicts

The v0.1.29 foundation source had five Python components that its
\`copy_foundation\` initializer omitted from the generated-file list. The
completed Panel initializer had nevertheless copied those files unchanged
into its committed \`main\`. When the newer initializer began managing them,
the three-way planner had \`old=None\`, \`new=foundation file\`, and
\`app=existing file\`, which looked like an overlapping addition.

For a fixed five-path allowlist, the coordinator now reads the **immutable**
old foundation source (already proven to belong to official main first-parent
history), applies only the historical app-init literal substitutions using
the validated Panel parameters, and compares the resulting bytes with the
tracked application \`main\` file. Only exact matches become an old baseline
for ordinary three-way planning. A byte mismatch, absent source blob,
unrecognized file, genuine overlapping edits, or non-regular file remains
blocked. No guessed SHA, force push, or partial provenance metadata is used.

This is a local source-planning fix; the existing Mac mini scheduler copies
its Python modules independently and MUST be upgraded from an accepted
foundation main release before it can apply this behavior. The scheduler
then retries formerly blocked plans rather than permanently caching them.
A successful code PR is **not** proof that the Panel public app has been
upgraded or deployed.


## v0.1.33: two remaining real-world Panel upgrade blockers

After updating the copied Mac mini scheduler runtime to accepted v0.1.32,
the user's live Panel page reported two remaining items:
\`deploy/macmini/manage.sh\` (review) and \`tests/test_auth.py\` (conflict).

For \`manage.sh\`, the earlier identity checker matched both the true,
unindented instance declarations and indented temporary assignments inside
the preview and acceptance conditionals. It now verifies only unindented
instance settings; it **still** rejects duplicated top-level declarations,
changed port/label/branch/origin, invalid Bash and overlapping three-way edits.

For \`tests/test_auth.py\`, the official foundation test source is identical
between Panel's registered old source and the newer target. Only
\`copy_foundation\`'s generated-application test transformations changed.
The already-committed Panel test was explicitly adapted to application
scope. In this narrowly limited case, the coordinator may retain the
Panel-owned test file **without staging any change to that path**, if:
the repo is the approved Office Panel; the immutable old/new official
foundation test blobs are identical; the current app test parses as Python;
and its named project-info test still asserts application scope, the Panel
project identity and the application development loop. Real upstream test
changes, absent assertions, malformed Python or any other app remain
subject to normal conflict handling.

Both changes require a fresh MacCI pass, base preview acceptance and
official main publication before a reviewed Mac mini scheduler runtime
upgrade. The scheduler is a separate installed copy. Only the next
*real host tick* can prove whether the blocked list becomes empty and
an actual Panel base-upgrade PR is created. Do not mark Panel as deployed
on the basis of planning or a PR alone.
