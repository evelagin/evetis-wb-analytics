# AE v1 — publisher credential: least-privilege design (PR #202 blocker 3)

Status: **DESIGN ONLY.** Nothing is created: no GitHub App, no key, no secret, no repository setting.
Owner decision recorded 2026-09-28: do NOT enable "Allow GitHub Actions to create and approve pull
requests" while a narrower credential exists.

## 1. What the publisher needs (and nothing more)

The trusted `publish` job of `autonomy-run.yml` (main only, after `gate` and `audit`) does three things:

| action | today | needs |
|---|---|---|
| push candidate to `refs/heads/ae/*` | `GITHUB_TOKEN` `contents: write` (publisher.py refuses any other ref) | unchanged |
| `gh pr create --draft` (publisher.py allow-list: `pr list`, `pr create --draft`, `workflow run`) | `GITHUB_TOKEN` `pull-requests: write` → **rejected**: repo setting off (`can_approve_pull_request_reviews=false`) | a credential that may create a PR |
| dispatch required workflows on the candidate branch | `GITHUB_TOKEN` `actions: write` | unchanged |

Only the second row is blocked. The design therefore replaces the credential of **that one call only**.

## 2. Options (verified 2026-09-28 against official docs)

| | A. `GITHUB_TOKEN` + repo setting | B. GitHub App installation token | C. no credential: owner opens the PR |
|---|---|---|---|
| Mechanism | Settings → Actions → "Allow GitHub Actions to create and approve pull requests" | `actions/create-github-app-token@v3` in the `publish` job, token passed only to `gh pr create` | publisher pushes `ae/*` and stops; owner runs `gh pr create --draft` |
| Scope | **Every workflow** using `GITHUB_TOKEN` in the repo gains **create AND approve** — one toggle, not separable ([docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)) | One repository, one job, permissions chosen per token: `pull-requests: write` + `contents: read` ([action](https://github.com/actions/create-github-app-token): `repositories`, `permission-*` inputs) | none |
| Can approve PRs | yes (all workflows) | yes — `POST …/pulls/{n}/reviews` needs the same `Pull requests: write` as create ([permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)); an App cannot approve its own PR | no |
| Can merge | no (merge needs contents write via the PR API = `GITHUB_TOKEN` already has it in publish; the setting does not add merge) | **no** — merge needs `Contents: write`, not granted | no |
| Lifetime | per job | **1 hour**, revoked in the action's post step unless `skip-token-revoke` ([action](https://github.com/actions/create-github-app-token)) | — |
| New long-lived secret | none | App private key (see §4) | none |
| CI on the PR | a `GITHUB_TOKEN`-created PR triggers no workflows (publisher dispatches them) | App-created PRs trigger `pull_request` workflows normally | normal |
| Blast radius if the workflow is compromised | any workflow could approve any PR | the publish step could create/approve PRs in this repo for ≤1 h | none |

Rejected alternatives: fine-grained PAT (user-bound, up to 1 year, acts as the owner — cannot even stay
"not the approver"); deploy key (git push only, cannot create PRs); classic PAT (repo-wide scopes).

## 3. Recommendation

**B — GitHub App, minimal permissions, token minted per publish step.** It is strictly narrower than A on
every axis (one job instead of every workflow; 1-hour revoked token; no merge; no Actions/secrets/settings
admin). C stays the zero-credential fallback and is what happens automatically if the App is absent
(publisher already stops at BLOCKED with the branch pushed).

### App specification (owner creates it; nothing is created by this PR)

| item | value |
|---|---|
| Owner | user `evelagin` (personal App, not public, "Only on this account") |
| Installation | **only** repository `evelagin/evetis-wb-analytics` ("Only select repositories") |
| Repository permissions | `Pull requests: Read and write`; `Contents: Read-only`; `Metadata: Read-only` (mandatory) |
| All other permissions | **No access** — in particular Administration, Actions, Secrets, Environments, Workflows, Checks, Contents write, Members, Organization anything |
| Webhook | disabled (no URL, no secret) |
| Events | none |
| Callback / device flow / user authorization | disabled — installation tokens only |
| Token request in the job | `repositories: evetis-wb-analytics`, `permission-pull-requests: write`, `permission-contents: read` (explicit, even though the installation allows no more) |
| Token lifetime | 1 h, revoked at job end (default, `skip-token-revoke` must NOT be set) |

### Acquisition flow (to be implemented in a follow-up PR after the owner creates the App)

```yaml
publish:
  environment: ae-publisher            # §4: the key is an environment secret
  permissions: {contents: write, actions: write}      # pull-requests: write is REMOVED from GITHUB_TOKEN
  steps:
    - uses: actions/create-github-app-token@<pinned SHA of v3>
      id: app
      with:
        client-id: ${{ vars.AE_PUBLISHER_APP_CLIENT_ID }}
        private-key: ${{ secrets.AE_PUBLISHER_APP_KEY }}
        repositories: evetis-wb-analytics
        permission-pull-requests: write
        permission-contents: read
    - name: publish
      env:
        GH_TOKEN: ${{ github.token }}                  # push ae/* + workflow run (unchanged)
        AE_PR_TOKEN: ${{ steps.app.outputs.token }}    # ONLY for `gh pr create --draft` / `gh pr list`
```

`publisher.py` passes `AE_PR_TOKEN` as `GH_TOKEN` to the two `gh pr` invocations only (its allow-list stays
`pr list`, `pr create --draft`, `workflow run`); the token never reaches any other process. A test pins that
`pr create` without `--draft`, `pr review`, `pr merge`, `api` remain refused.

## 4. Private-key custody (owner choice)

| | B1. environment secret (recommended) | B2. Cloud KMS, non-exportable |
|---|---|---|
| Storage | Actions **environment** secret `AE_PUBLISHER_APP_KEY` in environment `ae-publisher`: deployment branch rule `main` only; required reviewer = owner (on Free: public repositories only, see below) | GitHub-generated PEM imported once into Cloud KMS (`RSA_SIGN_PKCS1_2048_SHA256`, import job, protection SOFTWARE/HSM); local PEM destroyed |
| Who can use it | only a job that declares `environment: ae-publisher` on `main`, after the owner approves the deployment | only a WIF-bound SA with `roles/cloudkms.signerVerifier` on that one key version, bound to `autonomy-run.yml@main` job `publish` |
| Code | official action, no crypto code | ~40 lines: build App JWT, `asymmetricSign`, exchange for installation token (replaces the action) |
| New IAM | none | new SA + KMS keyring/key + WIF binding (Terraform, owner ACK) |
| Static secret in GitHub | yes (environment-scoped) | none |

B1 keeps the official action and adds a human approval on every publication (which matches the draft-PR
contract). B2 removes the static secret entirely at the cost of custom signing code and new IAM.

**Plan constraint (verified, [docs](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)):**
on GitHub Free, environment secrets, deployment branch rules and required reviewers exist **only for public
repositories**. The repository is public only temporarily (Actions billing, 2026-09-27). If it returns to
private on Free, B1 is impossible (a plain repository secret would reach every workflow — not acceptable):
then **B2 is the only acceptable custody**, or GitHub Pro.

## 5. Rotation, revocation, audit

* Rotation: generate a new key in the App settings → replace the environment secret (B1) / import a new
  KMS key version (B2) → delete the old key in the App settings. Suggested every 90 days.
* Emergency revocation: suspend or uninstall the App installation (immediate; outstanding tokens stop
  working); delete the key.
* Audit: every PR/review by the App appears as `<app-slug>[bot]` in the repository timeline and the
  account security log (installation token creation); environment deployments are logged with approver.
  A check in the follow-up PR: ci-verify refuses a PR whose author is not the App or whose `isDraft` is false
  (already enforced for the current flow).

## 6. Threat model (residual)

| threat | mitigation | residual |
|---|---|---|
| compromised publish step approves an unrelated PR | token ≤1 h, one job, environment approval (B1); App cannot approve its own PR; main has no required approvals, owner merges manually | LOW — an App approval grants no merge right |
| token exfiltrated | scoped to one repo, PR permissions only, revoked at job end | LOW |
| key exfiltrated (B1) | environment secret only to `main` + owner approval; rotation | MEDIUM without B2; LOW with B2 |
| App granted more permissions later | permission change requires the owner to accept it on the installation; follow-up PR adds a check of the installation permissions (`GET /repos/{r}/installation`) before minting | LOW |
| setting A enabled "temporarily" | not needed any more; keep `can_approve_pull_request_reviews=false` and assert it in ci-verify | — |

## 7. Exact owner actions (manual; STOP until done)

1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App with §3 values; generate a private key.
2. Install the App on `evelagin/evetis-wb-analytics` only.
3. B1: create environment `ae-publisher` (deployment branches: `main`; required reviewer: owner) and put the
   key in its secret `AE_PUBLISHER_APP_KEY`; set repository variable `AE_PUBLISHER_APP_CLIENT_ID`.
   (B2 instead: ACK for the KMS + SA + WIF Terraform, then import the key.)
4. ACK the follow-up PR that wires §3 into `autonomy-run.yml` / `publisher.py`.

Until then publication stays BLOCKED (option C: the owner can open the draft PR from the pushed `ae/*` branch).
