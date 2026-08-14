# Privacy boundary

`PalomarDatabaseTools` is intentionally public because it contains mechanisms,
not Palomar's confidential editorial or moderation data. Public CI must be able
to clone this repository without gaining any capability to read or mutate the
private registry.

## Safe to publish here

- deterministic validators and public projections;
- the public record schema and synthetic, invented fixtures;
- source-availability target and result schemas;
- the public-data Worker and its fake/local R2 tests; and
- dependency locks, build scripts, and CI configuration.

The synthetic score schema under `tests/fixtures/` exists only to exercise the
validator. It contains no review outcome and is not the canonical private
schema used by the registry.

## Must remain private

- `scores/` and the canonical score schema: the five review integers are not
  stable enough to bear the interpretation readers would put on them, and a
  digest would not conceal their small value space;
- `takedowns.json`, including reasons, identities bound to individual actions,
  and the private authorization issues behind those actions;
- unpublished entries, evidence, renders, registration projections, and state;
- moderation request/authorization automation, whose trust anchor is the
  private issue event and private repository history; and
- GitHub, Cloudflare, R2, OpenAI, and archive credentials.

The generic takedown manifest parser is public. It describes a file format and
contains the public governance roster; it does not contain any takedown row or
reason. The event-driven authorization and request handlers stay private.

## Why PalomarDatabase remains private

The database is more than the public records served at
`data.palomar-registry.org`. It is the canonical editorial ledger. It colocates
the public projection inputs with confidential review scores, reversible
takedown history and reasons, private authorization evidence, and material that
may be committed before publication. Publishing the repository would expose
those bytes and their Git history even if a later workflow omitted them from a
release. Git history also makes deletion after an accidental disclosure an
unreliable privacy control.

Therefore publication is a one-way, allow-listed projection from the private
ledger. Code is developed and tested here with synthetic data; the private
repository installs an immutable artifact and applies it to the real ledger.

## Enforcement

Public workflows have no credential for a private Palomar repository. They use
only synthetic fixtures or already-public HTTP documents.

Production deployment has no approval step of its own: every successful `CI`
run on a push to `main` deploys the Worker for that commit. The control is
branch protection on `main`, which requires a pull request, requires `python`,
`runtime-artifact`, `worker`, and `register` to pass on an up-to-date branch,
requires linear history and resolved conversations, forbids force pushes and
branch deletion, and applies to administrators. The `production` environment
holds the Cloudflare credential and accepts only protected branches; it adds no
reviewer. `CODEOWNERS` requests Technical Maintainer review on every change,
and on this file in particular, but that approval is not currently one of the
merge requirements.

Source-availability writes are limited to one Worker route, authenticated with
a dedicated secret, bounded to 5 MiB, schema-checked, target-set-checked, and
conditionally written.

Before copying future code or fixtures here, search the candidate tree for
real Palomar identifiers, score documents, takedown reasons, tokens, private
GitHub URLs, and repository exports. If a test needs a private fact, keep that
test private or replace the fact with an invented fixture.
