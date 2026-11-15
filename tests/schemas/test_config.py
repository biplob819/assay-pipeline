from pathlib import Path

import pytest

from assay_pipeline.config import ConfigError, load_build_config, load_views_config

DEFAULTS = Path(__file__).resolve().parents[2] / "src" / "assay_pipeline" / "defaults"


def test_default_build_config_is_egfr_ic50():
    cfg = load_build_config()
    t = cfg.target
    assert (t.name, t.chembl_target_id, t.uniprot, t.gene_symbol, t.activity_type) == (
        "EGFR",
        "CHEMBL203",
        "P00533",
        "EGFR",
        "IC50",
    )
    assert cfg.pubchem.max_requests_per_second <= 5  # PubChem usage policy
    assert cfg.pubchem.exclude_depositors == ("ChEMBL",)
    assert (cfg.quality.pic50_min, cfg.quality.pic50_max) == (3.0, 12.0)


def test_default_views_config_limits_and_views():
    cfg = load_views_config()
    lim = cfg.limits
    assert (lim.max_rows_executed, lim.max_rows_shown, lim.max_chars_shown) == (1000, 50, 6000)
    assert (lim.timeout_seconds, lim.step_budget) == (10, 8)
    assert cfg.view_names == ("v_activities", "v_compounds", "v_consensus", "v_quality")
    acts = next(v for v in cfg.views if v.name == "v_activities")
    assert "assay_description" in acts.columns and "variant" in acts.columns
    assert "read_csv" not in cfg.functions and "query_table" not in cfg.functions


def test_another_target_is_a_config_change(tmp_path):
    text = (DEFAULTS / "build.yaml").read_text(encoding="utf-8")
    text = text.replace("CHEMBL203", "CHEMBL1824").replace("P00533", "P04626")
    text = text.replace("name: EGFR", "name: ERBB2").replace(
        "gene_symbol: EGFR", "gene_symbol: ERBB2"
    )
    p = tmp_path / "erbb2.yaml"
    p.write_text(text, encoding="utf-8")
    t = load_build_config(p).target
    assert (t.name, t.chembl_target_id, t.uniprot, t.gene_symbol) == (
        "ERBB2",
        "CHEMBL1824",
        "P04626",
        "ERBB2",
    )


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda t: t + "extra_key: 1\n", "extra_key"),
        (lambda t: t.replace("page_size: 1000", "page_size: 5000"), "chembl.page_size"),
        (
            lambda t: t.replace("max_requests_per_second: 4", "max_requests_per_second: 10"),
            "pubchem.max_requests_per_second",
        ),
        (
            lambda t: t.replace("chembl_target_id: CHEMBL203", "chembl_target_id: 203"),
            "target.chembl_target_id",
        ),
        (lambda t: t.replace("pic50_min: 3.0", "pic50_min: 13.0"), "pic50_min must be below"),
        (lambda t: t.replace("target:\n", "target:\n  name: EGFR\n", 1), "duplicate key"),
        (lambda t: "- just\n- a list\n", "mapping at the top level"),
        (lambda t: "target: [unclosed\n", "not valid YAML"),
    ],
)
def test_build_config_errors_name_the_file_and_field(tmp_path, edit, needle):
    p = tmp_path / "build.yaml"
    p.write_text(edit((DEFAULTS / "build.yaml").read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_build_config(p)
    assert str(p) in str(exc.value) and needle in str(exc.value), str(exc.value)


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda t: t.replace("max_rows_shown: 50", "max_rows_shown: 5000"), "cannot exceed"),
        (lambda t: t.replace("name: v_compounds", "name: v_activities"), "unique names"),
        (lambda t: t.replace("name: v_quality", "name: quality"), "views.3.name"),
        (lambda t: t.replace("[count, sum,", "[count, count, sum,"), "functions must be unique"),
        (lambda t: t.replace("memory_limit: 1GB", "memory_limit: lots"), "limits.memory_limit"),
    ],
)
def test_views_config_errors(tmp_path, edit, needle):
    p = tmp_path / "views.yaml"
    p.write_text(edit((DEFAULTS / "views.yaml").read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_views_config(p)
    assert needle in str(exc.value), str(exc.value)


def test_missing_directory_and_non_utf8_files(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_build_config(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError, match="is a directory"):
        load_build_config(tmp_path)
    p = tmp_path / "latin1.yaml"
    p.write_bytes("target: caf\xe9\n".encode("latin-1"))
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_build_config(p)


@pytest.mark.parametrize("bad", ["read_csv", "query_table", "glob", "duckdb_tables", "getenv"])
def test_views_config_rejects_functions_outside_the_known_set(tmp_path, bad):
    p = tmp_path / "views.yaml"
    text = (DEFAULTS / "views.yaml").read_text(encoding="utf-8")
    p.write_text(text.replace("[count, sum,", f"[count, {bad}, sum,"), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"functions: Value error, functions not allowed"):
        load_views_config(p)


def test_config_floats_reject_booleans_and_strings(tmp_path):
    for value in ("true", "'4'"):
        p = tmp_path / "build.yaml"
        text = (DEFAULTS / "build.yaml").read_text(encoding="utf-8")
        p.write_text(
            text.replace(
                "max_requests_per_second: 4\n  exclude",
                f"max_requests_per_second: {value}\n  exclude",
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="max_requests_per_second"):
            load_build_config(p)


def test_default_functions_are_all_known():
    from assay_pipeline.config import KNOWN_FUNCTIONS

    assert set(load_views_config().functions) <= KNOWN_FUNCTIONS


@pytest.mark.contract
def test_built_wheel_contains_the_default_configs(tmp_path):
    """SCH-04: a pip install must carry the packaged defaults."""
    import shutil
    import subprocess
    import zipfile

    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    root = DEFAULTS.parents[2]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    assert {"assay_pipeline/defaults/build.yaml", "assay_pipeline/defaults/views.yaml"} <= names
