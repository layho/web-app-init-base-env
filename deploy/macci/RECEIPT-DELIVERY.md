# Independent MacCI receipt delivery

After the MacCI runtime containing `--receipts-only` has passed trusted review and has been installed, obtain the SHA-256 of `runner.py` from an independently verified, reviewed checkout (not from the untrusted installed file), then run `bash deploy/macci/install-receipt-delivery.sh <verified-sha256>` once. Python 3.10+ is required. The installer refuses an existing plist; inspect existing services before retrying. This is an operator-controlled installation, not an automatic update or an independent trust anchor. The installer verifies the installed runner and same-user private credential, then creates a separate LaunchAgent. It does not replace or restart the test worker.

The receipt agent invokes `runner.py --receipts-only` every 60 seconds. It uses the receipt-delivery lock, so it can send pending deployment and failure reports while candidate tests hold the worker lock; it does not invoke Docker or run candidate tests. A delivery failure returns nonzero and remains queued for retry.

Verify with `launchctl print gui/$(id -u)/com.layho.macci-receipt-delivery` and inspect `~/.local/share/macmini-pr-tests/receipt-delivery.log`. For a manual run, use `python3 ~/.local/share/macmini-pr-tests/runner.py --receipts-only`. Do not load this agent until the installed runner is the reviewed version. Real offline/restart recovery still needs a host validation record.


## Single-maintainer unattended preview policy

After explicit operator approval of this host-runtime change and installation, the administrator may opt in using root-owned read-only /usr/local/etc/macmini-pr-tests/preview-auto-paths.json, schema {"schema":1,"paths":["docs/guide.md"]}. No file means existing owner-only gate. Only exact listed existing ordinary documentation markdown and frontend CSS modifications are eligible in this initial rollout. The actual tested merge tree is checked locally, not a PR label or description. Any new/deleted/renamed file, mode change, unlisted or sensitive path requires exact owner approval. Explicit owner denial overrides policy. MacCI exact receipt, branch protection, non-force push and acceptance freeze are unchanged. No automatic replacement of frozen candidates. No independent agent identity required. This does not authorize application main or host upgrades; those remain separately controlled.
