# Operations

## Runtime artifact

The `runtime-artifact` CI job builds the wheel twice under one
`SOURCE_DATE_EPOCH`, compares the bytes, and uploads the wheel, reviewed
dependency lock, manifest, and checksums. A private consumer records both the
source commit and the wheel SHA-256 in `tooling.lock.json` before changing its
pin.

## Worker deployment

Create the `production` GitHub environment with required reviewers and add
`CLOUDFLARE_ACCOUNT_ID` and a narrowly scoped `CLOUDFLARE_API_TOKEN`. Dispatch
`Deploy public-data Worker` with the exact green commit. The workflow verifies
that the commit belongs to `main`, repeats Worker tests, and deploys both the
inert and production environments.

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
