# Star-history updater setup

The weekly `Update star history` workflow generates the light and dark charts and pushes a commit directly to the default branch using the STEMMechanics Repo Automation GitHub App. It stages only the two generated SVG files.

## One-time setup for this repository

1. Install the GitHub App on the STEMMechanics organization with access to this repository.
2. Add the app to the `Protect main` ruleset bypass list with **Always** mode. This lets the app bypass the pull-request and required-check rules when its token pushes the generated charts.
3. In **Settings > Secrets and variables > Actions**, add:
   - `STEMMECHANICS_REPO_AUTOMATION_APP_ID`: the GitHub App ID.
   - `STEMMECHANICS_REPO_AUTOMATION_PRIVATE_KEY`: the contents of a private key generated for the app. Keep it secret.

The workflow creates a short-lived installation token scoped to the Craftarr repository and the contents permission. It does not need the installation ID, webhook, or user authorization.

Run **Actions > Update star history > Run workflow** on the default branch to verify setup. If the generated charts are unchanged, the workflow exits without a commit.

The app bypass applies to any workflow that can obtain its token. Keep the private-key secret limited to repositories and workflows that need it.