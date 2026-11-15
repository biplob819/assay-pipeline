# B5.3 acceptance criteria

**REL-01** The README has one diagram, a headline results table generated from the published bundle (a test fails if it is stale), and a three-line rebuild that re-renders the dashboard byte for byte.
**REL-02** ChEMBL, PubChem and BindingDB attribution and the CC BY-SA 3.0 licence are in the README and the published data licence; code is MIT.
**REL-03** The published run commits the bundle, both audit anchors files (hashes only) and the verify result, and a test ties them to the bundle's chain head.
**REL-04** The decision log and the published snapshot's data card are linked from the README, and the data card's figures match the bundle.
**REL-05** A sample of real answers and audit lines from public cases was read by hand before publishing; the reading and its findings are recorded, and a test keeps held-out ids out of the record.
**REL-06** Tag v0.1.0 (after the owner registers the PyPI trusted publisher).
**OSS-01** On a version tag, PyPI `assay-pipeline` is published by trusted publishing only, and `uvx --from assay-pipeline assay doctor` passes on Linux, macOS and Windows after release.
**OSS-02** `assay doctor` checks Python, keys (never printed), network reach and local state, with `--json`; every command takes `--json`.
**OSS-03** Docs cover a first snapshot with no keys, configuring another target, controlling cost and reading the dashboard, and every relative link in them resolves.
**OSS-04** CONTRIBUTING, SECURITY (key handling, private vulnerability reports), CODE_OF_CONDUCT, and issue and PR templates exist.
**OSS-05** The privacy statement (no telemetry; data goes only to ChEMBL, PubChem and the configured model providers) is in the README and SECURITY.
**OSS-06** A second target (ABL1) builds end to end from config in CI on recorded real responses.
