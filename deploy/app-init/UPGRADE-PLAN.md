# Foundation upgrade planning

This is the read-only planning stage of Issue #6. Compare templates rendered with the same app parameters at an exact old and new source commit with the application's committed tree. The future PR coordinator will verify the source is an ancestor of accepted foundation main, verify the old parameter/provenance evidence, and stage only approved resulting files at a pinned application HEAD. This utility never creates or merges a PR, updates the production working tree, modifies app release notes or changes the recorded foundation version.

Usage:
```sh
python3 deploy/app-init/upgrade_plan.py --old-generated /tmp/old --new-generated /tmp/new --app /tmp/app-checkout
```

Output contains path/action/reason and SHA-256 digests only, not file contents. Non-overlapping text changes use Git three-way merge. New app-only files, app description, release history and initialization metadata are protected. Overlapping changes, source deletion, deployment script changes, and migrations require review; no unresolved plan may be used for unattended deployment. Current app source is read from committed Git blobs, never from local working files. Test: python -m pytest -q tests/test_upgrade_plan.py.
