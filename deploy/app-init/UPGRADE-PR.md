# Foundation upgrade PR coordinator

Use this reviewed development-host utility, not the production app web process. The application still owns release approval and deployment; the tool creates *draft* PRs only.

```sh
python3 deploy/app-init/upgrade_pr.py --repo OWNER/APP
python3 deploy/app-init/upgrade_pr.py --repo OWNER/APP --legacy-base EXACT_OLD_SHA
python3 deploy/app-init/upgrade_pr.py --repo OWNER/APP --create-pr
```

Requires Git, Python 3.10+, existing SSH read/write access to the chosen app repo and a dedicated GitHub REST API token with Pull requests write for PR creation. No new credentials, launchd jobs or database changes. Uses fresh temporary clones and only renders original generators from the accepted foundation main first-parent history. For a legacy app, verifies the entire root commit is exactly reproducible, including generation parameters. The plan is read-only by default. If source/provenance mismatches, overlapping changes, host/deployment changes, deletions or migrations require review, no branch or PR is created. For clean plans, a separate Git index constructs a commit preserving unrelated app files, updates .app-init.json provenance in the same commit, checks the official main and app main again, and pushes only a dedicated base-upgrade/<target SHA> branch. Existing PRs are reused; a matching existing branch is recovered if a prior PR creation failed. No force push or main update.

A draft PR is not an installed upgrade. App-specific review, actual MacCI tests, application deployment and public running SHA verification remain mandatory. Unresolved plans must be reviewed before adding a complete-target provenance record; never label a partially migrated app as fully upgraded. Automatic scheduled invocation and manual conflict resolution are separate next steps.
