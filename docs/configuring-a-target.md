# Configuring another target

Any protein with ChEMBL and PubChem activity data can be built (OWNER D-22). Write out the default config, then change its `target` block:

```bash
python -c "from importlib.resources import files; print(files('assay_pipeline').joinpath('defaults/build.yaml').read_text(), end='')" > abl1.yaml
```

The default lives in the package at `assay_pipeline/defaults/build.yaml`. Only the `target` block needs to change:

```yaml
target:
  name: ABL1                     # used in Activity.target and on the data card
  chembl_target_id: CHEMBL1862   # the single-protein ChEMBL target
  uniprot: P00519                # PubChem assays are kept only when they name this accession
  gene_symbol: ABL1              # PubChem assays are found by gene symbol
  activity_type: IC50            # IC50, Ki, Kd or EC50; values are normalised to nM and p-value
```

Then pass it to every stage, with a cache folder of its own (a cache is tied to one target, and reusing the EGFR cache is refused):

```bash
assay ingest chembl --config abl1.yaml --cache cache-abl1
assay ingest pubchem --config abl1.yaml --cache cache-abl1
assay snapshot --config abl1.yaml --cache cache-abl1 --out snapshots-abl1
```

Check the target first: the ChEMBL target must be a single protein (not a complex or family), and the UniProt accession must match it. PubChem assays that name the target only by a RefSeq accession are dropped with the reason `target_not_configured` (a stated limit on the data card). The `quality` block sets the QC3 range; widen it for activity types whose plausible values differ from IC50.

The attack suite and the normal questions are EGFR benchmark cases: the normal questions' gold results are tied to the published EGFR snapshot, so `assay attack` on another target runs the attacks as they are and computes gold from that target's database.
