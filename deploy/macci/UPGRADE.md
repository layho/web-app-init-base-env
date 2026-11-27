# MacCI host maintenance

The foundation application and trusted host services have separate lifecycles. A preview deployment does not replace files in ~/.local/share/macmini-pr-tests. Use only a pinned reviewed commit and explicitly verified test receipt. This revision keeps existing launchd labels, token, Docker isolation and SSH. V0.5.0 updates runner.py, merge_preview.py and the independently reviewed preview_journal.py dependency. The installed main publisher is compatible with the updated coordinator helpers; do not replace or restart the publisher for this fix.

Run candidate regression tests only in the isolated MacCI environment, never on the credential-bearing host.
## Trusted runtime installer

`upgrade-runtime.py` in a candidate checkout is source data only. Never execute that copy on the host. `--check` and `--apply` must be invoked through the root-owned fixed installer at `/usr/local/libexec/macmini-pr-tests/runtime-installer.py`; it verifies its own SHA-256 against the separate root-owned read-only pin at `/usr/local/etc/macmini-pr-tests/runtime-installer.sha256` before parsing arguments or reading the candidate checkout. The fixed installer reads candidate blobs from `~/web-app-init-base-env-preview` by exact commit SHA, disables Git replace objects and system/global configuration, compiles modules without executing them, and only applies the three modules after independent digests are provided.

Initial provisioning or changing the installer itself is a deliberate administrator operation after independent review of the exact installer bytes. For a reviewed commit, install the file and its separately recorded digest with root ownership and no write bits:

```sh
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/macmini-pr-tests /usr/local/etc/macmini-pr-tests
sudo install -o root -g wheel -m 0555 deploy/macci/upgrade-runtime.py /usr/local/libexec/macmini-pr-tests/runtime-installer.py
shasum -a 256 /usr/local/libexec/macmini-pr-tests/runtime-installer.py | awk '{print $1}' > /tmp/runtime-installer.sha256
sudo install -o root -g wheel -m 0444 /tmp/runtime-installer.sha256 /usr/local/etc/macmini-pr-tests/runtime-installer.sha256
rm -f /tmp/runtime-installer.sha256
```

After provisioning, invoke only the fixed path, as the MacCI user (not with `sudo`):

```sh
/usr/bin/python3 -I /usr/local/libexec/macmini-pr-tests/runtime-installer.py --sha <reviewed-main-SHA> --check
/usr/bin/python3 -I /usr/local/libexec/macmini-pr-tests/runtime-installer.py --sha <reviewed-main-SHA> --runner-sha256 <independent-runner-digest> --merge-sha256 <independent-merge-digest> --journal-sha256 <independent-journal-digest> --apply
```

The installer uses the existing lock files, rejects concurrent jobs, backs up existing runtime files, replaces each file atomically under the existing locks, and does not install a LaunchAgent, change credentials, or stop processes. The next existing scheduled invocation reads the new script. If a service is busy, it exits without replacing files; retry once idle. Afterward verify actual MacCI statuses and preview branch advancement, not only process registration. Do not install unreviewed code or execute arbitrary issue text.


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
- **Upgrade order:** publish the exact MacCI-tested official main SHA; obtain independent reviewed module digests, then invoke the fixed installer with `-I` as documented above. Do not run any candidate checkout installer or candidate tests on the host. Human functional acceptance may follow publication; host authorization remains separate.
- **Host acceptance:** confirm the installed
  `~/.local/share/macmini-pr-tests/runner.py` advertises protocol 1; trigger
  a manual test only against a newly eligible exact SHA/base/merge, confirm
  `lock_handoff=true`, and inspect its actual MacCI success/failure status.
  A launch response by itself is not proof that tests ran.

Never erase the current worker lock file to work around contention, reuse a
stale commit status, or release another candidate while preview is frozen.


V0.5.0 requires independent review and re-pinning of the fixed installer before using the three-module upgrade path. The journal dependency is installed first; rollback removes it if it did not previously exist. This does not initialize an active managed queue or activate MACCI_MANAGED_QUEUE. Existing launchd configuration and credentials remain unchanged. Do not execute the candidate installer directly or self-approve the new pin.
