.PHONY: verify-audit test

# Re-hash an audit log and check its anchors (US5). Usage: make verify-audit LOG=audit/run.jsonl
LOG ?= tests/fixtures/audit_ok.jsonl
verify-audit:
	uv run assay verify-audit "$(LOG)"

test:
	uv run pytest -q
