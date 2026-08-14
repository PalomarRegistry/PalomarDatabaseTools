# Palomar Database Tools

Public, data-independent tooling for the [Palomar Registry](https://palomar-registry.org/):

- validators and public-data builders used by `PalomarDatabase`;
- synthetic contract tests shared with the submission and reviewer systems;
- the Cloudflare Worker serving `data.palomar-registry.org`; and
- the bounded source-availability monitor.

This repository contains no Palomar review scores, takedown records or reasons,
private moderation issues, unpublished registration state, or production
credentials. The boundary and its rationale are documented in
[`docs/privacy-boundary.md`](docs/privacy-boundary.md).

## Reproducible runtime

Every commit on `main` builds a wheel and a hash-locked runtime bundle. Private
callers pin a commit and the bundle digest in their own `tooling.lock.json`;
they never execute the moving tip of this repository.

```console
python -m pip install --require-hashes --no-deps -r requirements-test.txt
python -m pytest -q
python -m palomar_database_tools validate --root /path/to/database
```

Worker development is independent:

```console
cd worker
npm ci
npm run check
npm test
npm run test:runtime
```

The software in this repository is licensed under the MIT License. The
database records remain governed by the license in `PalomarDatabase`; moving
code here does not relicense any record or linked work.
