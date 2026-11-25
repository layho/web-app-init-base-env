# Independent MacCI receipt delivery

After the MacCI runtime containing `--receipts-only` has passed trusted review and has been installed, run `bash deploy/macci/install-receipt-delivery.sh` once from the reviewed checkout. The installer verifies the installed runner and same-user private credential, then creates a separate LaunchAgent. It does not replace or restart the test worker.

The receipt agent invokes `runner.py --receipts-only` every 60 seconds. It uses the receipt-delivery lock, so it can send pending deployment and failure reports while candidate tests hold the worker lock; it does not invoke Docker or run candidate tests. A delivery failure returns nonzero and remains queued for retry.

Verify with `launchctl print gui/$(id -u)/com.layho.macci-receipt-delivery` and inspect `~/.local/share/macmini-pr-tests/receipt-delivery.log`. For a manual run, use `python3 ~/.local/share/macmini-pr-tests/runner.py --receipts-only`. Do not load this agent until the installed runner is the reviewed version. Real offline/restart recovery still needs a host validation record.
