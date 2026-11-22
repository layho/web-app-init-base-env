# MacCI host maintenance

The foundation application and trusted host services have separate lifecycles. A preview deployment does not replace files in ~/.local/share/macmini-pr-tests. Use only a pinned reviewed commit and explicitly verified test receipt. This revision keeps existing launchd labels, token, Docker isolation and SSH. It changes only runner.py and merge_preview.py. The installed main publisher is compatible with the updated coordinator helpers; do not replace or restart the publisher for this fix.

Run tests from the reviewed checkout:
```sh
cd deploy/macci
python3 -m unittest -v test_runner test_merge_preview test_publish_main
```
For maintenance, use upgrade-runtime.py with --check to verify prerequisites and tests, or --apply after confirming the exact source SHA and test receipt. It uses the existing lock files and rejects concurrent jobs. It backs up the two runtime files, atomically replaces them, and does not install a new LaunchAgent, change credentials or stop any processes. The next existing scheduled invocation reads the new script. If a service is busy, it exits without replacing files; retry once idle. Afterward verify actual MacCI statuses and preview branch advancement, not only process registration. Do not install unreviewed code or execute arbitrary issue text.
