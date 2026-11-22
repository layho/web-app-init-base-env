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
