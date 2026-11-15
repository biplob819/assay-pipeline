"""N1.4: normalise both recorded sources into Activity rows (cache only, no network)."""

from __future__ import annotations

import json

import pytest

from assay_pipeline.pipeline import normalise as nm
from assay_pipeline.pipeline.ingest.cache import RawCache
from assay_pipeline.pipeline.ingest.chembl import IngestError, ingest_chembl, read_chembl
from assay_pipeline.pipeline.ingest.pubchem import ingest_pubchem
from assay_pipeline.pipeline.normalise import (
    StructureError,
    normalise,
    parse_value,
    pic50_for,
    round_sig,
    standardise,
    to_nm,
)
from assay_pipeline.schemas import Activity
from assay_pipeline.schemas.data import pic50_of
from tests.pipeline import test_ingest_chembl as chembl_t
from tests.pipeline import test_ingest_pubchem as pubchem_t

SNAP = "2026-11-15.1"


def cfg():
    return chembl_t.cfg()  # EGFR IC50 defaults, ChEMBL page size 5 for the recorded pages


def built(tmp_path, chembl_server=None, pubchem_server=None) -> RawCache:
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, chembl_t.fetcher(chembl_server or chembl_t.Server()))
    ingest_pubchem(cfg(), cache, pubchem_t.fetcher(pubchem_server or pubchem_t.Server()))
    return cache


# ---- NRM-01: structures (D-3) ----


def test_the_parent_is_the_largest_fragment_uncharged():
    smiles, key = standardise("O=C([O-])c1ccccc1.[Na+]")
    assert smiles == "O=C(O)c1ccccc1"
    assert key == standardise("OC(=O)c1ccccc1")[1]
    assert standardise("C[NH3+].[Cl-]")[0] == "CN"


def test_tautomers_are_not_canonicalised_but_inchi_folds_common_ones():
    a = standardise("Oc1ccccn1")
    b = standardise("O=c1cccc[nH]1")
    assert a[0] != b[0]  # no tautomer canonicalisation (D-3)
    assert a[1] == b[1]  # standard InChI folds this pair


@pytest.mark.parametrize("bad", ["C1CC", "not a smiles", "c1cccc1"])
def test_unreadable_structures_raise(bad):
    with pytest.raises(StructureError):
        standardise(bad)


def test_the_rdkit_version_is_reported(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    import rdkit

    assert result.rdkit_version == rdkit.__version__


# ---- NRM-02: units and rounding ----


@pytest.mark.parametrize(
    ("value", "units", "expected"),
    [
        (41.0, "nM", 41.0),
        (1500.0, "pM", 1.5),
        (0.0013, "uM", 1.3),
        (0.0013, "µM", 1.3),
        (0.0013, "μM", 1.3),
        (2.0, "mM", 2e6),
        (1e-9, "M", 1.0),
        (0.0, "nM", 0.0),
        (5.0, "ug.mL-1", None),
        (5.0, None, None),
        (None, "nM", None),
        (-3.0, "nM", None),
    ],
)
def test_units_convert_over_the_molar_set_only(value, units, expected):
    assert to_nm(value, units, 6) == expected


def test_value_nm_is_rounded_once_to_significant_figures():
    assert round_sig(123.4567891, 6) == 123.457
    assert round_sig(0.000123456789, 6) == 0.000123457
    assert to_nm(0.1234567891, "uM", 6) == 123.457
    assert to_nm(0.1234567891, "uM", 3) == 123.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("41.0", 41.0), (" 7 ", 7.0), (3, 3.0), ("", None), ("abc", None), ("nan", None),
     ("inf", None), (None, None), (True, None)],
)  # fmt: skip
def test_source_values_parse_to_finite_floats_or_none(raw, expected):
    assert parse_value(raw) == expected


# ---- NRM-03: pIC50 and relations ----


def test_pic50_comes_from_the_rounded_value():
    value_nm = to_nm(0.1234567891, "uM", 6)
    assert value_nm is not None
    assert pic50_for(value_nm) == pic50_of(123.457)
    assert pic50_for(0.0) is None and pic50_for(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("=", "="), ("<", "<"), (">", ">"), ("<=", "<="), (">=", ">="), ("~", "unknown"),
     (None, "unknown"), ("<<", "unknown")],
)  # fmt: skip
def test_chembl_relations_map_and_the_rest_are_unknown(tmp_path, raw, expected):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["standard_relation"] = raw
    rid = str(server.pages[0]["activities"][0]["activity_id"])
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    (row,) = [a for a in result.activities if a.source_record_id == rid]
    assert row.relation == expected


def test_every_pubchem_row_has_an_unknown_relation(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    pub = [a for a in result.activities if a.source == "pubchem"]
    assert pub and all(a.relation == "unknown" for a in pub)


# ---- NRM-04: variant and assay type ----


def test_variants_map_and_blank_means_wild_type(tmp_path):
    server = chembl_t.Server()
    acts = server.pages[0]["activities"]
    acts[0]["assay_variant_mutation"] = "L858R"
    acts[1]["assay_variant_mutation"] = "  "
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    by_id = {a.source_record_id: a for a in result.activities}
    assert by_id[str(acts[0]["activity_id"])].variant == "L858R"
    assert by_id[str(acts[1]["activity_id"])].variant == "wild-type/unspecified"
    pub = [a for a in result.activities if a.source == "pubchem"]
    assert all(a.variant == "wild-type/unspecified" and a.assay_type == "U" for a in pub)


def test_an_unknown_chembl_assay_type_is_dropped_with_a_reason(tmp_path):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["assay_type"] = "U"
    rid = str(server.pages[0]["activities"][0]["activity_id"])
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert rid not in {a.source_record_id for a in result.activities}
    assert result.drops_by_reason()["unknown_assay_type"] == 1


# ---- NRM-05: provenance ----


def test_every_row_carries_its_provenance(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    for a in result.activities:
        assert a.source_record_id and a.assay_id and a.snapshot == SNAP and a.target == "EGFR"
        if a.source == "chembl":
            assert a.document_id and a.document_id.startswith("CHEMBL") and a.depositor is None
            assert a.chembl_src_id == 1
        else:
            assert a.depositor == "BindingDB" and a.source_record_id.count(":") == 2
            assert a.document_id is None or a.document_id.startswith("PMID:")
    kept_comments = [a for a in result.activities if a.data_validity_comment]
    assert len(kept_comments) == 5  # "Outside typical range": kept and counted, not dropped


def test_uids_are_stable_hashes_of_source_and_record(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    for a in result.activities:
        assert a.activity_uid == nm.activity_uid(a.source, a.source_record_id or "")
    assert len({a.activity_uid for a in result.activities}) == len(result.activities)


# ---- NRM-06: drop ledger and source-key mismatches ----


def test_both_recorded_sources_normalise_end_to_end(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    assert result.rows_in == {"chembl": 15, "pubchem": 3}
    assert result.releases == {"chembl": "ChEMBL_37", "pubchem": "2026-11-15"}
    counts = {s: sum(a.source == s for a in result.activities) for s in ("chembl", "pubchem")}
    assert counts == {"chembl": 15, "pubchem": 3}
    assert all(isinstance(a, Activity) for a in result.activities)
    keys = [(a.inchikey, a.source, a.source_record_id, a.activity_uid) for a in result.activities]
    assert keys == sorted(keys)  # §5.5 row order
    by_reason = result.drops_by_reason()
    assert by_reason["chembl_deposited_aid"] == 1 and by_reason["other_target"] >= 10
    # every row offered to normalise is either an Activity or a drop at this stage
    stage_drops = sum(d.stage == "normalise" for d in result.drops)
    assert len(result.activities) + stage_drops == sum(result.rows_in.values())


def test_blank_and_unreadable_structures_are_dropped_with_reasons(tmp_path):
    server = chembl_t.Server()
    acts = server.pages[0]["activities"]
    acts[0]["canonical_smiles"] = ""
    acts[1]["canonical_smiles"] = "C1CC"
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    by_reason = result.drops_by_reason()
    assert by_reason["no_structure"] == 1 and by_reason["unparseable_structure"] == 1
    assert len([a for a in result.activities if a.source == "chembl"]) == 13


def test_source_key_mismatches_are_counted(tmp_path):
    first = normalise(cfg(), built(tmp_path / "a"), SNAP).source_key_mismatches
    server = pubchem_t.Server()
    for p in server.props["PropertyTable"]["Properties"]:
        if p["CID"] == 16126651:
            p["InChIKey"] = "AAAAAAAAAAAAAA-BBBBBBBBBB-N"  # a key that is not the parent's
    second = normalise(cfg(), built(tmp_path / "b", pubchem_server=server), SNAP)
    assert second.source_key_mismatches == first + 1


def test_normalise_is_deterministic(tmp_path):
    cache = built(tmp_path)
    assert normalise(cfg(), cache, SNAP) == normalise(cfg(), cache, SNAP)


def test_a_uid_collision_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(nm, "activity_uid", lambda _s, _r: "0" * 16)
    with pytest.raises(ValueError, match="collision"):
        normalise(cfg(), built(tmp_path), SNAP)


# ---- the ChEMBL reader ----


def test_the_chembl_reader_returns_every_record_once_in_order(tmp_path):
    release, rows = read_chembl(cfg(), built(tmp_path))
    ids = [r["activity_id"] for r in rows]
    assert release == "ChEMBL_37" and len(ids) == 15 and ids == sorted(set(ids))


def test_the_chembl_reader_refuses_an_incomplete_or_damaged_release(tmp_path):
    cache = built(tmp_path)
    cur = cache.read_cursor("chembl", "ChEMBL_37")
    assert cur is not None
    cache.write_cursor("chembl", "ChEMBL_37", {**cur, "total_count": 16})
    with pytest.raises(IngestError, match="16"):
        read_chembl(cfg(), cache)
    cache.write_cursor("chembl", "ChEMBL_37", {**cur, "complete": False})
    with pytest.raises(IngestError, match="no complete chembl release"):
        read_chembl(cfg(), cache)
    with pytest.raises(IngestError, match="not complete"):
        read_chembl(cfg(), cache, "ChEMBL_37")


def test_the_chembl_reader_refuses_another_target(tmp_path):
    cache = built(tmp_path)
    other = cfg().model_copy(
        update={"target": cfg().target.model_copy(update={"chembl_target_id": "CHEMBL1824"})}
    )
    with pytest.raises(IngestError, match="another target"):
        read_chembl(other, cache)


def test_a_record_cached_twice_is_refused(tmp_path):
    cache = built(tmp_path)
    index = cache.index("chembl", "ChEMBL_37")
    page_rows = [r for r in index.values() if "activity" in r.url]
    body = json.loads(cache.get("chembl", "ChEMBL_37", page_rows[0].request_key))
    extra = json.dumps(body).encode()
    cache.put(
        "chembl",
        "ChEMBL_37",
        "f" * 64,
        page_rows[0].url + "&dup=1",
        200,
        extra,
        "2026-11-15T13:24:00+00:00",
    )
    with pytest.raises(IngestError, match="cached twice"):
        read_chembl(cfg(), cache)


# ---- verifier pass 1 findings ----


@pytest.mark.parametrize("smiles", ["CCO c1ccccc1", "CCO\nc1ccccc1", "CCO\tCl"])
def test_a_smiles_with_whitespace_inside_is_never_half_read(tmp_path, smiles):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["canonical_smiles"] = smiles
    rid = str(server.pages[0]["activities"][0]["activity_id"])
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert rid not in {a.source_record_id for a in result.activities}
    assert any(
        d.source_record_id == rid and d.reason == "unparseable_structure" for d in result.drops
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assay_chembl_id", None),
        ("assay_chembl_id", ""),
        ("assay_chembl_id", 7),
        ("assay_variant_mutation", 5),
        ("assay_variant_mutation", "L858R\nT790M"),  # the schema refuses a newline in a Name
        ("standard_units", 5),
        ("standard_relation", ["="]),
        ("document_chembl_id", 123),
        ("src_id", -1),
        ("src_id", True),
        ("src_id", "1"),
        ("data_validity_comment", {"x": 1}),
        ("assay_description", ["x"]),
    ],
)
def test_a_malformed_chembl_record_is_dropped_with_a_reason_not_a_crash(tmp_path, field, value):
    server = chembl_t.Server()
    server.pages[0]["activities"][0][field] = value
    rid = str(server.pages[0]["activities"][0]["activity_id"])
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert rid not in {a.source_record_id for a in result.activities}
    assert any(d.source_record_id == rid and d.reason == "malformed_record" for d in result.drops)
    assert len(result.activities) == 17  # every other row is still there


@pytest.mark.parametrize("assay_type", [" B", "b", None, ["B"]])
def test_a_badly_typed_assay_type_is_an_unknown_assay_type(tmp_path, assay_type):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["assay_type"] = assay_type
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert result.drops_by_reason()["unknown_assay_type"] == 1


def test_a_malformed_pubchem_source_key_drops_that_row(tmp_path):
    server = pubchem_t.Server()
    for p in server.props["PropertyTable"]["Properties"]:
        if p["CID"] == 16126651:
            p["InChIKey"] = "not-an-inchikey"
    result = normalise(cfg(), built(tmp_path, pubchem_server=server), SNAP)
    assert result.drops_by_reason()["malformed_record"] == 1
    assert sum(a.source == "pubchem" for a in result.activities) == 2


@pytest.mark.parametrize("raw", ["1_000", "١٢٣", "²", "1,000", "0x10", "1e"])
def test_only_ascii_decimal_numbers_parse(raw):
    assert parse_value(raw) is None


def test_non_ascii_pubmed_ids_are_not_documents(tmp_path):
    server = pubchem_t.Server()
    cells = server.concise[1798578]["Table"]["Row"][0]["Cell"]
    cells[10] = "١٢٣"
    result = normalise(cfg(), built(tmp_path, pubchem_server=server), SNAP)
    row = next(a for a in result.activities if a.source_record_id == "1798578:251974395:1")
    assert row.document_id is None


def test_a_positive_value_that_underflows_is_not_stored_as_zero():
    assert to_nm(5e-324, "pM", 6) is None
    assert to_nm(1e300, "M", 6) is None
    assert to_nm(-0.0, "nM", 6) == 0.0 and str(to_nm(-0.0, "nM", 6)) == "0.0"


def test_validity_comments_are_counted(tmp_path):
    result = normalise(cfg(), built(tmp_path), SNAP)
    assert result.validity_comments == 5


def test_an_unsafe_release_name_is_an_ingest_error(tmp_path):
    with pytest.raises(IngestError, match="unsafe"):
        read_chembl(cfg(), built(tmp_path), "../x")


# ---- verifier pass 2 findings ----


@pytest.mark.parametrize("snapshot", ["not-a-snapshot", "2026-02-30.1", "2026-11-15"])
def test_a_bad_snapshot_id_is_raised_not_dropped(tmp_path, snapshot):
    with pytest.raises(ValueError):
        normalise(cfg(), built(tmp_path), snapshot)


def test_a_bad_target_name_is_raised_not_dropped(tmp_path):
    bad = cfg().model_copy(update={"target": cfg().target.model_copy(update={"name": "EGFR "})})
    with pytest.raises(ValueError):
        normalise(bad, built(tmp_path), SNAP)


def test_a_code_bug_is_raised_never_hidden_as_malformed_data(tmp_path, monkeypatch):
    cache = built(tmp_path)
    real = nm.pic50_for

    def off_by_a_tenth(v):
        got = real(v)
        return None if got is None else got + 0.1

    monkeypatch.setattr(nm, "pic50_for", off_by_a_tenth)
    with pytest.raises(ValueError, match="pic50"):
        normalise(cfg(), cache, SNAP)


def test_an_underflowing_source_string_has_no_value():
    assert parse_value("1e-400") is None
    assert parse_value("0e-400") == 0.0 and parse_value("0.000") == 0.0


def test_a_huge_json_integer_keeps_the_row_without_a_value(tmp_path):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["standard_value"] = 10**400
    rid = str(server.pages[0]["activities"][0]["activity_id"])
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    row = next(a for a in result.activities if a.source_record_id == rid)
    assert row.value_original is None and row.value_nm is None and row.pic50 is None


def test_the_public_standardise_never_half_reads():
    with pytest.raises(StructureError):
        standardise("CCO CCN")


def test_an_assay_id_with_whitespace_inside_is_malformed(tmp_path):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["assay_chembl_id"] = "CHEMBL1\nCHEMBL2"
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert result.drops_by_reason()["malformed_record"] == 1


def test_a_cursor_must_say_complete_with_a_real_true(tmp_path):
    cache = built(tmp_path)
    cur = cache.read_cursor("chembl", "ChEMBL_37")
    assert cur is not None
    cache.write_cursor("chembl", "ChEMBL_37", {**cur, "complete": "no"})
    with pytest.raises(IngestError, match="not complete"):
        read_chembl(cfg(), cache, "ChEMBL_37")


# ---- verifier pass 3 nice-to-haves ----


def test_a_mapping_bug_that_refuses_every_row_is_raised(tmp_path, monkeypatch):
    cache = built(tmp_path)
    monkeypatch.setattr(nm, "WILD_TYPE", " wild-type")  # not a valid Name
    with pytest.raises(ValueError, match="mapping bug"):
        normalise(cfg(), cache, SNAP)


def test_a_document_id_with_whitespace_inside_is_malformed(tmp_path):
    server = chembl_t.Server()
    server.pages[0]["activities"][0]["document_chembl_id"] = "CHEMBL1\nX"
    result = normalise(cfg(), built(tmp_path, chembl_server=server), SNAP)
    assert result.drops_by_reason()["malformed_record"] == 1
