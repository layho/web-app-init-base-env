# MacCI host maintenance

The foundation application and trusted host services have separate lifecycles. A preview deployment does not replace files in ~/.local/share/macmini-pr-tests. Use only a pinned reviewed commit and explicitly verified test receipt. This revision keeps existing launchd labels, token, Docker isolation and SSH. It changes only runner.py and merge_preview.py. The installed main publisher is compatible with the updated coordinator helpers; do not replace or restart the publisher for this fix.

Run tests from the reviewed checkout:
```sh
cd deploy/macci
python3 -m unittest -v test_runner test_merge_preview test_publish_main
```
For maintenance, use upgrade-runtime.py with --check to verify prerequisites and tests, or --apply after confirming the exact source SHA and test receipt. It uses the existing lock files and rejects concurrent jobs. It backs up the two runtime files, atomically replaces them, and does not install a new LaunchAgent, change credentials or stop any processes. The next existing scheduled invocation reads the new script. If a service is busy, it exits without replacing files; retry once idle. Afterward verify actual MacCI statuses and preview branch advancement, not only process registration. Do not install unreviewed code or execute arbitrary issue text.


## Manual test lock handoff (PR #95)

The web application and trusted host Runner have **separate** deployment lifecycles.
The web handler recognizes the installed Runner's `LOCK_HANDOFF_PROTOCOL = 1`
declaration only in a locally trusted, owner-controlled runner file.

- **Old host Runner:** the handler uses the original CLI arguments and explicitly
  reports `lock_handoff=false`; the old lock-check/spawn race remains possible.
  Never treat an HTTP 202 response as a successful test result.
- **Upgraded host Runner:** the handler passes a locked `worker.lock` file
  descriptor to the worker using `--lock-fd`. The worker checks the inode,
  device, owner and file mode before adopting it. The host's periodic invocation
  still acquires its own worker lock normally.
- **Upgrade order:** first merge the reviewed and MacCI-tested PR into preview,
  perform the normal preview deployment and human acceptance, then publish the
  exact accepted SHA to main. Only after that, on the Mac mini, use
  `python3 deploy/macci/upgrade-runtime.py --sha <accepted-main-SHA> --check`
  and then `--apply` from the checked-out matching commit. The script refuses
  active jobs, backs up existing host Runner and coordinator files, and does
  not change the GitHub token or Cloudflare services. Do not run an unreviewed
  preview copy with `--apply`.
- **Host acceptance:** confirm the installed
  `~/.local/share/macmini-pr-tests/runner.py` advertises protocol 1; trigger
  a manual test only against a newly eligible exact SHA/base/merge, confirm
  `lock_handoff=true`, and inspect its actual MacCI success/failure status.
  A launch response by itself is not proof that tests ran.

Never erase the current worker lock file to work around contention, reuse a
stale commit status, or release another candidate while preview is frozen.
