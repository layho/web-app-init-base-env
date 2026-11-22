# Independent foundation upgrade scheduler (macOS)

Only install from an exact accepted official main commit, after verifying MacCI tests. Existing MacCI, preview publishing, application updater, tokens and data are not replaced. Uses the existing SSH credentials and authenticated GitHub CLI; no sudo and no new token are created. The GUI launchd domain already used by MacCI can be addressed from SSH.

Run from the accepted foundation checkout:
```sh
python3 deploy/app-init/install-upgrade-scheduler.py --sha "$(git rev-parse HEAD)"
python3 deploy/app-init/install-upgrade-scheduler.py --sha "$(git rev-parse HEAD)" --apply
```
Register explicitly:
```sh
S="$HOME/.local/share/macmini-foundation-upgrades/runtime/upgrade_scheduler.py"
python3 "$S" register --repo layho/macmini-office-app-panel --mode create
python3 "$S" register --repo layho/xiongzhan-ti-hunter --mode observe
python3 "$S" register --repo layho/xiongzhan-clawer --mode observe
python3 "$S" list
python3 "$S" tick
```
Panel create mode requires merged, valid provenance and authenticated github PR write permission. Legacy apps without a valid reproducible old source remain observe until their own migration is reviewed and merged. Never invent a base SHA. Registered apps created later by app-init are enrolled automatically only if the scheduler is already installed. Existing manual registrations are not overwritten when rerunning initialization. One job, five-minute interval, no concurrent runs, one open upgrade PR per app. All PRs are draft; testing, review and application deployment remain separate. Failed network calls retry. Conflicts are held until actual app or official main SHA changes (or explicit tick --force). State lives in private ~/.local/share/macmini-foundation-upgrades; logs contain only sanitized outcomes and SHA/PR references. No force push, application main write, live database access, or runtime code hot reload. Check launchctl print gui/$(id -u)/com.layho.foundation-upgrades and scheduler.log for evidence.
