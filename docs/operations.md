# Operations

## Runtime artifact

The `runtime-artifact` CI job builds the wheel twice under one
`SOURCE_DATE_EPOCH`, compares the bytes, and uploads the wheel, reviewed
dependency lock, manifest, and checksums. A private consumer records both the
source commit and the wheel SHA-256 in `tooling.lock.json` before changing its
pin.

## Worker deployment

The repository has two GitHub Actions environments with deliberately different
credentials:

| GitHub environment | Environment secret | Used by |
| --- | --- | --- |
| `production` | `CLOUDFLARE_ACCOUNT_ID` | Worker deployment |
| `production` | `CLOUDFLARE_API_TOKEN` | Worker deployment |
| `source-availability-production` | `PALOMAR_AVAILABILITY_UPDATE_TOKEN` | Six-hourly availability refresh |

Do not put the Cloudflare deployment token in
`source-availability-production`. That environment is only for the bounded
availability writer.

The Cloudflare dashboard credential used here is named exactly
**PalomarDatabaseTools GitHub deploy**. It is a separate credential from the
pre-existing **palomar worker deployment token** used by another deployment
path. Do not edit, copy, or replace the latter while configuring this
repository. GitHub stores the value of **PalomarDatabaseTools GitHub deploy** as
the `CLOUDFLARE_API_TOKEN` secret in this repository's `production`
environment.

Create or edit **PalomarDatabaseTools GitHub deploy** from
<https://dash.cloudflare.com/?to=%2F%3Aaccount%2Fapi-tokens>. Cloudflare's
current custom-token editor requires both **Workers Scripts: Edit** for the
Palomar account and **Workers Routes: Edit** for the
`palomar-registry.org` zone. The latter is required because `wrangler deploy`
reconciles the production custom domain as well as uploading the script.
Cloudflare documentation calls the broader preset **Edit Cloudflare Workers**.
The deployment token needs no R2 object permission: the deployed Worker reaches
R2 through its binding, and the private publisher has a separate bucket-scoped
credential.

To install the token in GitHub's UI, open
<https://github.com/PalomarRegistry/PalomarDatabaseTools/settings/environments>,
select **production**, and add an environment secret named exactly
`CLOUDFLARE_API_TOKEN`. Do not rely on a guessed environment-edit URL; GitHub's
direct route varies with the viewer's settings context. As a CLI alternative,
with the value in a local shell variable that was not placed in shell history:

```console
printf '%s' "$CLOUDFLARE_API_TOKEN" | gh secret set CLOUDFLARE_API_TOKEN \
  --repo PalomarRegistry/PalomarDatabaseTools --env production
unset CLOUDFLARE_API_TOKEN
```

The `production` environment restricts deployments to protected branches and
has no required reviewers and no wait timer; administrators can bypass its
protection rules. Every successful `CI` run caused by a push to `main`
automatically triggers `Deploy public-data Worker` for that exact commit, so
branch protection on `main` is what decides which commits reach production.
The deployment workflow verifies that the green commit still belongs to `main`
and deploys both the inert and production Worker environments. It relies on the
Worker checks and dry-run deployments already completed by `CI` instead of
repeating them and consuming additional Actions minutes. The similarly named
GitHub and Wrangler environments are separate concepts.

## Source-availability writer

Generate one random token of at least 32 bytes. Store the same value as:

- the Worker secret `PALOMAR_AVAILABILITY_UPDATE_TOKEN` in both Worker
  environments; and
- the Actions secret of that name in the
  `source-availability-production` GitHub environment.

For example, with the value in a local shell variable (never in shell history):

```console
cd worker
printf '%s' "$TOKEN" | npx wrangler secret put PALOMAR_AVAILABILITY_UPDATE_TOKEN
printf '%s' "$TOKEN" | npx wrangler secret put PALOMAR_AVAILABILITY_UPDATE_TOKEN --env production
printf '%s' "$TOKEN" | gh secret set PALOMAR_AVAILABILITY_UPDATE_TOKEN \
  --repo PalomarRegistry/PalomarDatabaseTools --env source-availability-production
```

The scheduled workflow can write only through the Worker's bounded operation.
The Worker authenticates before reading the body, requires an R2 conditional
write, limits the body to 5 MiB, validates schema version 2, and requires the
result rows and `targets_sha256` to match the currently served target document.

`PalomarDatabase` remains the only producer of the target document. Its small
target-publication job uses the existing R2 publication credential and exposes
only repository names and commit IDs already present in public records. The
public job performs the numerous GitHub availability probes every six hours.

The schedule is deliberately inert during rollout. After the Worker is
deployed, the target document is present, and both copies of the writer secret
are configured, enable it with:

```console
gh variable set SOURCE_AVAILABILITY_ENABLED \
  --repo PalomarRegistry/PalomarDatabaseTools --body true
```
