# Contributing

Pull requests must keep all fixtures synthetic and must not require access to a
private Palomar repository. Run the Python and Worker suites described in the
README. Generated dependency locks and Worker types must be committed with the
change that updates their inputs.

Changes affecting deployed Worker behavior are reviewed by a Palomar Technical
Maintainer, whom `CODEOWNERS` requests on every pull request. Merging is the
deployment decision: once the required checks on `main` pass, the push deploys
the Worker with no further approval. Merging still does not hand a contributor
a production credential; the Cloudflare token stays in this repository's
`production` environment.
