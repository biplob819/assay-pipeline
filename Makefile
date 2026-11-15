.PHONY: verify-audit test ingest snapshot publish build-db

# Re-hash an audit log and check its anchors (US5). Usage: make verify-audit LOG=audit/run.jsonl
LOG ?= tests/fixtures/audit_ok.jsonl
verify-audit:
	uv run assay verify-audit "$(LOG)"

# Fetch both sources into the raw cache (the only stage that uses the network, D-2).
ingest:
	uv run assay ingest chembl
	uv run assay ingest pubchem

# Normalise, flag and write a snapshot from the cache; run it twice and the manifest is
# identical (US3). Usage: make snapshot [CONFIG=other.yaml]
CONFIG ?=
snapshot:
	uv run assay snapshot $(if $(CONFIG),--config "$(CONFIG)",)

# Copy a verified, unplanted snapshot to published/. Usage: make publish SNAPSHOT=snapshots/<id>
publish:
	uv run assay publish "$(SNAPSHOT)"

# Build agent.duckdb from a snapshot. Usage: make build-db SNAPSHOT=snapshots/<id>
build-db:
	uv run assay build-db "$(SNAPSHOT)"

test:
	uv run pytest -q
