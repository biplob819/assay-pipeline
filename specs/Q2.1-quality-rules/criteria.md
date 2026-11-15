# Q2.1 acceptance criteria

**QCR-01** QC1 flags a row whose nM value cannot be computed (missing value, missing units, units outside the molar set), with a reason naming the cause, and passes a converted value including zero; each case has a pass, a fail and a boundary test.
**QCR-02** QC2 flags every row of a key `(inchikey, target, variant, assay_type)` that has at least two `=` rows with a pIC50 spread above 1.0, compared at pIC50 precision so exactly 1.0 is not a conflict; different keys never conflict.
**QCR-03** QC3 flags a present pIC50 outside the configured bounds (default 3 and 12, both inclusive), censored rows included, and a value of exactly 0 nM (D-29), with boundary tests at exactly 3 and 12.
**QCR-04** QC4 flags a row whose source record id is missing or blank, and no other.
**QCR-05** QC5 flags every relation other than `=`, censored and unknown alike; rules flag and never delete or reorder rows, and every flag has exactly one reason.
**QCR-06** The consensus table has one row per key with the median and spread over `=` rows with a pIC50, conflict exactly when QC2 flags the key, and every member uid; a table mixing snapshots is refused.
**QCR-07** Ten LLM04 planted-row cases over QC1–QC4 each pass in a scratch build of real rows (every planted row carries every expected flag), switching off any one of QC1–QC4 fails exactly that rule's cases (every row expects its case's rule), and three stealth rows are reported as known misses.
