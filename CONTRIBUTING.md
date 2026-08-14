# Contributing

Pull requests must keep all fixtures synthetic and must not require access to a
private Palomar repository. Run the Python and Worker suites described in the
README. Generated dependency locks and Worker types must be committed with the
change that updates their inputs.

For changes affecting deployed Worker behavior, `CODEOWNERS` requests review
from a Palomar Technical Maintainer, though branch protection does not require
that approval. Merging is the deployment decision: the merge pushes to `main`,
and the push-triggered `CI` run, if it succeeds, starts deployment with no
further approval. Merging still does not hand a contributor a production
credential; the Cloudflare token stays in this repository's `production`
environment.
