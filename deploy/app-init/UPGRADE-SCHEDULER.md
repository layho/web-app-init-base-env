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
