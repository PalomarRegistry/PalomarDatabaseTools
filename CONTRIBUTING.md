# Contributing

Pull requests must keep all fixtures synthetic and must not require access to a
private Palomar repository. Run the Python and Worker suites described in the
README. Generated dependency locks and Worker types must be committed with the
change that updates their inputs.

Changes affecting deployed Worker behavior require review by a Palomar
Technical Maintainer. Deployment is a separate, reviewed environment action;
merging code does not implicitly grant a contributor a production credential.
