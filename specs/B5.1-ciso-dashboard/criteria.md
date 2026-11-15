# B5.1 acceptance criteria

**DSH-01** `assay report` renders one self-contained static HTML page with inline SVG and no scripts or external resources from a results bundle, and re-renders it byte for byte from the committed bundle.
**DSH-02** A "Did anything get through?" panel shows outcomes by category and agent with each success rate's 95% upper bound, zero successes stated as a bound, pass^k, and held-out results flagged beside public ones.
**DSH-03** A "What stopped it?" panel shows the stopping layer by category for the main run per agent, the guard-off ablation and the model-free oracle side by side, with a legend and a table view.
**DSH-04** Access events show the audit log's verify status, its anchors and latest anchor, and recent audit lines from public cases only, never held-out text.
**DSH-05** A data-quality tab shows flags per rule, drops per reason, rows by source and the snapshot history from the manifests.
**DSH-06** Every mark has a hover and keyboard-focus label and every chart a table view or legend, light and dark modes are both chosen, and a banner marks the restricted table as synthetic.
**DSH-07** A Pages workflow re-renders the dashboard from the committed bundle, fails if the committed page differs, and deploys it, with SHA-pinned actions and least privilege.
