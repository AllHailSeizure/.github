# AllHailSeizure defaults

Shared defaults for AllHailSeizure repositories.

- **Community health files**: pull request template, issue forms, `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`. GitHub applies these to any repo without its own copy.
- **Labels**: [`labels.yml`](labels.yml) is the account-wide label scheme, applied by the manual label-sync workflow.
- **Automerge**: a reusable workflow that merges `automerge`-labelled PRs once required checks pass.
- **Governance separation**: a reusable check that blocks PRs mixing governance files ([`governance.json`](governance.json)) with other changes.
