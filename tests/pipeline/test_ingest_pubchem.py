"""N1.3: PubChem ingest on recorded real responses, through a mock transport (no network)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from assay_pipeline.config import ConfigError, load_build_config
from assay_pipeline.pipeline.ingest import pubchem
from assay_pipeline.pipeline.ingest.cache import RawCache, request_key
from assay_pipeline.pipeline.ingest.chembl import IngestError
from assay_pipeline.pipeline.ingest.http import Fetcher
from assay_pipeline.pipeline.ingest.pubchem import (
    DepositorTerms,
    ingest_pubchem,
    load_depositor_terms,
    read_pubchem,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pubchem"
DATE = "Sun, 15 Nov 2026 13:07:30 GMT"
RELEASE = "2026-11-15"
KEPT = (1794871, 1798470, 1798578)


def load(name: str):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


class Server:
    """Serves the recorded fixture by path; can inject failures, edits and pages."""

    def __init__(self) -> None:
        self.aids = load("aids")
        self.summary = load("summary")
        self.chembl = load("chembl_src7_assays")
        self.concise = {a: load(f"concise_{a}") for a in KEPT}
        self.props = load("properties")
        self.log: list[httpx.Request] = []
        self.fail: dict[str, list[int | str]] = {}  # path fragment -> queued failures

    def _ok(self, data) -> httpx.Response:
        return httpx.Response(200, json=data, headers={"Date": DATE})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        path = request.url.path
        for frag, queue in self.fail.items():
            if frag in path and queue:
                f = queue.pop(0)
                if f == "transport":
                    raise httpx.ConnectError("planted", request=request)
                if f == "busy":
                    fault = {"Fault": {"Code": "PUGREST.ServerBusy", "Message": "Too many"}}
                    return httpx.Response(503, json=fault, headers={"Date": DATE})
                return httpx.Response(int(f), json={"Fault": {}}, headers={"Date": DATE})
        if path.endswith("/aids/JSON"):
            return self._ok(self.aids)
        if path.endswith("/summary/JSON"):
            asked = {int(a) for a in path.split("/")[-3].split(",")}
            rows = [r for r in self.summary["AssaySummaries"]["AssaySummary"] if r["AID"] in asked]
            return self._ok({"AssaySummaries": {"AssaySummary": rows}})
        if path.endswith("/assay.json"):
            return self._ok(self.chembl)
        if path.endswith("/concise/JSON"):
            return self._ok(self.concise[int(path.split("/")[-3])])
        if "/property/" in path:
            asked = {int(c) for c in path.split("/")[-4].split(",")}
            props = [p for p in self.props["PropertyTable"]["Properties"] if p["CID"] in asked]
            return self._ok({"PropertyTable": {"Properties": props}})
        return httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})

    def paths(self, frag: str) -> list[str]:
        return [r.url.path for r in self.log if frag in r.url.path]


def fetcher(server: Server, **kw) -> Fetcher:
    return Fetcher(transport=httpx.MockTransport(server), sleep=lambda _s: None, **kw)


def cfg():
    return load_build_config()


def reasons(report) -> dict[str, int]:
    return report.drops_by_reason


# ---- PUB-01..04: discovery, decisions, concise rows, structures ----


def test_full_ingest_decides_every_aid_and_keeps_only_target_ic50_rows(tmp_path):
    server = Server()
    cache = RawCache(tmp_path)
    report = ingest_pubchem(cfg(), cache, fetcher(server))
    assert (report.release, report.aids_listed, report.aids_kept, report.rows) == (
        RELEASE,
        6,
        3,
        3,
    )
    read = read_pubchem(cfg(), cache)
    decided = {d.aid: d.reason for d in read.decisions}
    assert decided == {
        1433: "target_not_configured",
        1729: "chembl_imported_aid",
        8621: "chembl_deposited_aid",
        1794871: None,
        1798470: None,
        1798578: None,
    }
    assert {r.source_record_id for r in read.rows} == {
        "1798470:56269724:8",  # this SID has 8 rows in the table, one per target
        "1798578:251974395:1",
        "1798578:381611587:1",
    }
    assert all(r.activity_name == "IC50" and r.depositor == "BindingDB" for r in read.rows)
    assert {r.cid: r.inchikey for r in read.rows}[16126651] == "XYFPWWZEPKGCCK-UHFFFAOYSA-N"
    assert {r.source_record_id: r.value_um for r in read.rows}["1798578:251974395:1"] == "0.0013"
    assert reasons(report)["not_activity_type"] == 4  # AID 1794871's EGFR rows are Kd
    assert reasons(report)["other_target"] >= 10
    assert all(d.stage == "ingest" and d.source == "pubchem" for d in read.drops)


def test_the_aid_list_is_for_the_configured_gene_symbol(tmp_path):
    server = Server()
    ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert server.paths("/aids/JSON") == ["/rest/pug/assay/target/genesymbol/EGFR/aids/JSON"]
    chembl = next(r for r in server.log if r.url.path.endswith("/assay.json"))
    assert chembl.url.params["target_chembl_id"] == "CHEMBL203"
    assert chembl.url.params["src_id"] == "7"


def test_dropped_aids_never_have_their_data_fetched(tmp_path):
    server = Server()
    ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    fetched = {int(p.split("/")[-3]) for p in server.paths("/concise/JSON")}
    assert fetched == set(KEPT)


def test_structures_are_fetched_in_batches_for_kept_rows_only(tmp_path, monkeypatch):
    monkeypatch.setattr(pubchem, "PROPERTY_BATCH", 2)
    server = Server()
    ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    asked = [p.split("/")[-4] for p in server.paths("/property/")]
    assert asked == ["11667893,16126651", "118519887"]  # sorted; the Kd-only CIDs are not asked


def test_summaries_are_fetched_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(pubchem, "SUMMARY_BATCH", 4)
    server = Server()
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert len(server.paths("/summary/JSON")) == 2 and report.aids_kept == 3


def test_a_compound_with_no_structure_is_dropped_with_a_reason(tmp_path):
    server = Server()
    server.props["PropertyTable"]["Properties"] = [
        p for p in server.props["PropertyTable"]["Properties"] if p["CID"] != 118519887
    ]
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert report.rows == 2 and reasons(report)["no_structure"] == 1


def test_a_row_with_no_cid_is_dropped_with_a_reason(tmp_path):
    server = Server()
    server.concise[1798578]["Table"]["Row"][0]["Cell"][2] = ""
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert report.rows == 2 and reasons(report)["no_cid"] == 1


def test_a_split_src_assay_id_counts_as_its_aid(tmp_path):
    server = Server()
    server.aids["IdentifierList"]["AID"].append(493040)
    server.summary["AssaySummaries"]["AssaySummary"].append(
        {"AID": 493040, "SourceName": "BindingDB", "Target": [{"Accession": "P00533"}]}
    )
    ingest_pubchem(cfg(), cache := RawCache(tmp_path), fetcher(server))
    decided = {d.aid: d.reason for d in read_pubchem(cfg(), cache).decisions}
    assert decided[493040] == "chembl_imported_aid"  # from src_assay_id "493040_69"


def test_refseq_only_targets_are_dropped_as_a_stated_limit(tmp_path):
    server = Server()
    server.chembl["assays"] = [a for a in server.chembl["assays"] if a["src_assay_id"] != "1729"]
    server.chembl["page_meta"]["total_count"] = len(server.chembl["assays"])
    ingest_pubchem(cfg(), cache := RawCache(tmp_path), fetcher(server))
    decided = {d.aid: d.reason for d in read_pubchem(cfg(), cache).decisions}
    assert decided[1729] == "target_not_configured"  # NP_005219 is EGFR, but not P00533


def test_the_chembl_import_list_is_paged(tmp_path, monkeypatch):
    monkeypatch.setattr(pubchem, "CHEMBL_PAGE", 2)
    server = Server()
    full = server.chembl["assays"]

    def paged(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assay.json"):
            server.log.append(request)
            off = int(request.url.params["offset"])
            page = {"assays": full[off : off + 2], "page_meta": {"total_count": len(full)}}
            return httpx.Response(200, json=page, headers={"Date": DATE})
        return server(request)

    f = Fetcher(transport=httpx.MockTransport(paged), sleep=lambda _s: None)
    ingest_pubchem(cfg(), cache := RawCache(tmp_path), f)
    offsets = [r.url.params["offset"] for r in server.log if r.url.path.endswith("/assay.json")]
    assert offsets == ["0", "2", "4"]
    decided = {d.aid: d.reason for d in read_pubchem(cfg(), cache).decisions}
    assert decided[1729] == "chembl_imported_aid"


# ---- PUB-05: rate limit and ServerBusy ----


def test_server_busy_is_retried_with_backoff(tmp_path):
    server = Server()
    server.fail["/concise/"] = ["busy", "busy"]
    sleeps: list[float] = []
    f = Fetcher(transport=httpx.MockTransport(server), sleep=sleeps.append)
    report = ingest_pubchem(cfg(), RawCache(tmp_path), f)
    assert report.rows == 3
    assert len(server.paths("/concise/JSON")) == 3 + 2
    assert sum(s >= 0.25 for s in sleeps) >= 2  # two backoff waits


def test_requests_stay_under_five_per_second(tmp_path):
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    server = Server()
    rps = cfg().pubchem.max_requests_per_second
    f = Fetcher(
        transport=httpx.MockTransport(server),
        sleep=sleep,
        clock=lambda: now[0],
        max_requests_per_second=rps,
    )
    ingest_pubchem(cfg(), RawCache(tmp_path), f)
    n = len(server.log)
    assert rps < 5 and now[0] >= (n - 1) / rps - 1e-9  # paced over the whole run


def test_the_config_refuses_five_or_more_requests_per_second():
    c = cfg().model_dump()
    c["pubchem"]["max_requests_per_second"] = 5.0
    with pytest.raises(ValueError, match="less than 5"):
        type(cfg()).model_validate(c)


def test_cli_paces_pubchem_with_its_own_rate(tmp_path, monkeypatch, capsys):
    from assay_pipeline import cli

    seen = []
    server = Server()

    def make(rps):
        seen.append(rps)
        return fetcher(server)

    monkeypatch.setattr(cli, "default_fetcher", make)
    code = cli.main(["ingest", "pubchem", "--cache", str(tmp_path / "c"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and seen == [cfg().pubchem.max_requests_per_second]
    assert out["ok"] and out["source"] == "pubchem" and out["rows"] == 3
    assert out["drops_by_reason"]["chembl_deposited_aid"] == 1
    assert out["requests_made"] == 7 and out["cached_hits"] == 0


# ---- PUB-06: the depositor ledger ----


def test_unlisted_depositors_are_dropped_and_their_data_never_fetched(tmp_path):
    server = Server()
    none = DepositorTerms(depositors=())
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server), terms=none)
    assert report.rows == 0 and reasons(report)["depositor_not_cleared"] == 3
    assert server.paths("/concise/") == []


def test_a_depositor_listed_with_keep_false_is_dropped(tmp_path):
    terms = load_depositor_terms()
    off = terms.model_copy(
        update={"depositors": tuple(d.model_copy(update={"keep": False}) for d in terms.depositors)}
    )
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(Server()), terms=off)
    assert reasons(report)["depositor_not_cleared"] == 3


def test_the_shipped_ledger_clears_bindingdb_with_evidence():
    terms = load_depositor_terms()
    (bdb,) = [d for d in terms.depositors if d.name == "BindingDB"]
    assert bdb.keep and bdb.evidence and "CC BY" in bdb.licence and bdb.attribution
    assert "ChEMBL" not in terms.cleared()


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("depositors:\n  - {name: A, keep: true, licence: x, attribution: y, "
         "evidence: [https://a], checked: 2026-11-15}\n"
         "  - {name: A, keep: true, licence: x, attribution: y, "
         "evidence: [https://a], checked: 2026-11-15}\n", "listed twice"),
        ("depositors:\n  - {name: A, keep: true, licence: x, attribution: y, "
         "evidence: [http://a], checked: 2026-11-15}\n", "invalid"),
        ("depositors:\n  - {name: A, keep: yes please, licence: x, attribution: y, "
         "evidence: [https://a], checked: 2026-11-15}\n", "invalid"),
        ("depositors:\n  - {name: A, keep: true, licence: x, attribution: y, "
         "evidence: [], checked: 2026-11-15}\n", "invalid"),
        ("depositors: []\ndepositors: []\n", "invalid"),
    ],
    ids=["duplicate", "http-evidence", "bad-bool", "no-evidence", "duplicate-key"],
)  # fmt: skip
def test_a_malformed_ledger_is_refused(tmp_path, text, match):
    p = tmp_path / "terms.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_depositor_terms(p)


def test_a_missing_ledger_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_depositor_terms(tmp_path / "nope.yaml")


# ---- resume, releases and the cache ----


def test_a_failed_run_resumes_without_refetching_and_matches_a_clean_run(tmp_path):
    server = Server()
    server.fail["/concise/JSON"] = [404]  # not retried: the run stops part-way
    with pytest.raises(Exception, match="404"):
        ingest_pubchem(cfg(), RawCache(tmp_path / "a"), fetcher(server))
    before = len(server.log)
    report = ingest_pubchem(cfg(), RawCache(tmp_path / "a"), fetcher(server))
    again = server.log[before:]
    assert not any(r.url.path.endswith(("/aids/JSON", "/summary/JSON")) for r in again)
    assert report.release == RELEASE and report.cached_hits >= 3
    ingest_pubchem(cfg(), RawCache(tmp_path / "b"), fetcher(Server()))
    assert cache_digest(tmp_path / "a") == cache_digest(tmp_path / "b")


def test_a_complete_release_is_a_no_op_until_new_release(tmp_path):
    cache = RawCache(tmp_path)
    ingest_pubchem(cfg(), cache, fetcher(Server()))
    server = Server()
    report = ingest_pubchem(cfg(), cache, fetcher(server))
    assert server.log == [] and report.requests_made == 0 and report.rows == 3
    fresh = ingest_pubchem(cfg(), cache, fetcher(Server()), new_release=True)
    assert fresh.release == f"{RELEASE}.2"
    assert read_pubchem(cfg(), cache).release == f"{RELEASE}.2"
    assert read_pubchem(cfg(), cache, RELEASE).rows == read_pubchem(cfg(), cache).rows


def test_new_release_abandons_an_incomplete_one(tmp_path):
    server = Server()
    server.fail["/concise/JSON"] = [404]
    with pytest.raises(Exception, match="404"):
        ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(Server()), new_release=True)
    assert report.release == f"{RELEASE}.2"
    cursor = RawCache(tmp_path).read_cursor("pubchem", RELEASE)
    assert cursor is not None and cursor["abandoned"] is True
    with pytest.raises(IngestError, match="not complete"):
        read_pubchem(cfg(), RawCache(tmp_path), RELEASE)


def test_a_cache_built_for_another_target_is_refused(tmp_path):
    cache = RawCache(tmp_path)
    ingest_pubchem(cfg(), cache, fetcher(Server()))
    other = cfg().model_copy(
        update={"target": cfg().target.model_copy(update={"gene_symbol": "ERBB2"})}
    )
    with pytest.raises(IngestError, match="another --cache folder"):
        ingest_pubchem(other, cache, fetcher(Server()))


def test_a_malformed_response_is_never_cached(tmp_path):
    server = Server()
    del server.summary["AssaySummaries"]["AssaySummary"][3]["SourceName"]
    cache = RawCache(tmp_path)
    with pytest.raises(IngestError, match="no SourceName"):
        ingest_pubchem(cfg(), cache, fetcher(server))
    urls = [r.url for r in cache.index("pubchem", RELEASE).values()]
    assert not any("summary" in u for u in urls)
    report = ingest_pubchem(cfg(), cache, fetcher(Server()))  # a healthy rerun completes
    assert report.rows == 3


@pytest.mark.parametrize(
    ("name", "edit", "match"),
    [
        ("aids", lambda d: d["IdentifierList"].update(AID=[1, 1]), "listed twice"),
        ("aids", lambda d: d["IdentifierList"].update(AID=["1433"]), "positive integers"),
        ("summary", lambda d: d["AssaySummaries"]["AssaySummary"][1].update(AID=1433),
         "came twice"),
        ("chembl", lambda d: d["assays"][0].update(src_assay_id="CHEMBL1"), "not a PubChem AID"),
        ("concise", lambda d: d["Table"]["Columns"]["Column"].remove("CID"), "missing"),
        ("concise", lambda d: d["Table"]["Row"][0]["Cell"].pop(), "do not match"),
        ("concise", lambda d: d["Table"]["Row"][0]["Cell"].__setitem__(0, "1"), "belongs to"),
        ("props", lambda d: d["PropertyTable"]["Properties"].append(
            dict(d["PropertyTable"]["Properties"][0])), "came twice"),
        ("concise", lambda d: d["Table"]["Columns"]["Column"].__setitem__(1, "AID"), "repeats"),
        ("concise", lambda d: d["Table"]["Row"][0]["Cell"].__setitem__(6, 1.5), "not a string"),
    ],
)  # fmt: skip
def test_each_response_kind_is_checked_before_caching(tmp_path, name, edit, match):
    server = Server()
    target = {
        "aids": server.aids,
        "summary": server.summary,
        "chembl": server.chembl,
        "concise": server.concise[1798578],
        "props": server.props,
    }[name]
    edit(target)
    with pytest.raises(IngestError, match=match):
        ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))


def test_the_reader_needs_no_network_and_refuses_another_ledger(tmp_path):
    cache = RawCache(tmp_path)
    ingest_pubchem(cfg(), cache, fetcher(Server()))
    first = read_pubchem(cfg(), cache)
    assert read_pubchem(cfg(), cache) == first  # deterministic, cache only (sockets blocked)
    for other in (DepositorTerms(depositors=()), widened()):
        with pytest.raises(IngestError, match="--new-release"):
            read_pubchem(cfg(), cache, terms=other)  # never a silent, different answer


def widened() -> DepositorTerms:
    base = load_depositor_terms().depositors[0]
    return DepositorTerms(
        depositors=(*load_depositor_terms().depositors, base.model_copy(update={"name": "NCGC"}))
    )


def test_a_response_without_a_date_cannot_name_a_release(tmp_path):
    def no_date(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("aids"))

    f = Fetcher(transport=httpx.MockTransport(no_date), sleep=lambda _s: None)
    with pytest.raises(IngestError, match="no Date header"):
        ingest_pubchem(cfg(), RawCache(tmp_path), f)


def test_a_concurrent_ingest_is_refused(tmp_path):
    cache = RawCache(tmp_path)
    cache.lock("pubchem")
    with pytest.raises(Exception, match="another ingest"):
        ingest_pubchem(cfg(), cache, fetcher(Server()))


def test_cached_keys_match_the_requests_made(tmp_path):
    server = Server()
    cache = RawCache(tmp_path)
    ingest_pubchem(cfg(), cache, fetcher(server))
    index = cache.index("pubchem", RELEASE)
    assert len(index) == len(server.log)
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/target/genesymbol/EGFR/aids/JSON"
    assert request_key("GET", url, {}) in index


# ---- golden ----

GOLDEN = FIX / "expected_cache.sha256"


def cache_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file() and x.name != ".lock"):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        data = p.read_bytes()
        h.update(gzip.decompress(data) if p.suffix == ".gz" else data)
    return h.hexdigest()


GOLDEN_TERMS = DepositorTerms.model_validate(
    {
        "depositors": [
            {
                "name": "BindingDB",
                "keep": True,
                "licence": "CC BY",
                "attribution": "BindingDB",
                "evidence": ["https://www.bindingdb.org/rwd/bind/info.jsp"],
                "checked": "2026-11-15",
            }
        ]
    }
)  # fixed here, so editing the shipped ledger is not a golden format change


@pytest.mark.golden
def test_golden_pubchem_ingest_reproduces_the_recorded_cache(tmp_path):
    """The recorded fixture always ingests to the same bytes; a change here is a format change."""
    ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(Server()), terms=GOLDEN_TERMS)
    assert cache_digest(tmp_path) == GOLDEN.read_text(encoding="utf-8").strip()


# ---- verifier pass 1 findings ----


def test_rows_sharing_a_sid_get_their_own_record_ids(tmp_path):
    """Real AID 1794871 repeats SID 354327356 for two Kd measurements (0.054 and 0.27 uM)."""
    kd = cfg().model_copy(
        update={"target": cfg().target.model_copy(update={"activity_type": "Kd"})}
    )
    ingest_pubchem(kd, cache := RawCache(tmp_path), fetcher(Server()))
    rows = [r for r in read_pubchem(kd, cache).rows if r.aid == 1794871]
    ids = [r.source_record_id for r in rows]
    assert len(ids) == len(set(ids)) == 4
    assert {r.source_record_id: r.value_um for r in rows} == {
        "1794871:354327356:1": "0.054",
        "1794871:354327356:2": "0.27",
        "1794871:354327357:1": "0.0063",
        "1794871:354327357:2": "0.0093",
    }


@pytest.mark.parametrize("sid", ["", "abc def", "0", "-1"])
def test_a_row_without_a_usable_sid_is_dropped(tmp_path, sid):
    server = Server()
    server.concise[1798578]["Table"]["Row"][0]["Cell"][1] = sid
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert report.rows == 2 and reasons(report)["no_sid"] == 1


def test_cid_zero_is_not_a_compound(tmp_path):
    server = Server()
    server.concise[1798578]["Table"]["Row"][0]["Cell"][2] = "0"
    report = ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    assert report.rows == 2 and reasons(report)["no_cid"] == 1
    assert all(",0" not in p and "/0/" not in p for p in server.paths("/property/"))


def test_a_cid_sent_without_an_inchikey_is_dropped_not_fatal(tmp_path):
    server = Server()
    for p in server.props["PropertyTable"]["Properties"]:
        if p["CID"] == 11667893:
            del p["InChIKey"]
    cache = RawCache(tmp_path)
    report = ingest_pubchem(cfg(), cache, fetcher(server))
    assert report.rows == 2 and reasons(report)["no_structure"] == 1
    assert read_pubchem(cfg(), cache).rows == read_pubchem(cfg(), cache).rows


def test_a_properties_batch_pubchem_does_not_know_is_dropped_not_fatal(tmp_path):
    server = Server()

    def not_found(request: httpx.Request) -> httpx.Response:
        if "/property/" in request.url.path:
            server.log.append(request)
            fault = {"Fault": {"Code": "PUGREST.NotFound", "Message": "No CID found"}}
            return httpx.Response(404, json=fault, headers={"Date": DATE})
        return server(request)

    f = Fetcher(transport=httpx.MockTransport(not_found), sleep=lambda _s: None)
    cache = RawCache(tmp_path)
    report = ingest_pubchem(cfg(), cache, f)
    assert report.rows == 0 and reasons(report)["no_structure"] == 3
    assert read_pubchem(cfg(), cache).rows == ()  # the 404 is cached, so the reader agrees


def test_another_404_fault_is_still_an_error(tmp_path):
    server = Server()
    server.fail["/property/"] = [404]  # a 404 without the NotFound fault
    with pytest.raises(IngestError, match="unexpected PubChem response"):
        ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))


def test_a_gene_with_no_assays_gets_a_clear_error(tmp_path):
    def none(request: httpx.Request) -> httpx.Response:
        fault = {"Fault": {"Code": "PUGREST.NotFound", "Message": "No data found"}}
        return httpx.Response(404, json=fault, headers={"Date": DATE})

    f = Fetcher(transport=httpx.MockTransport(none), sleep=lambda _s: None)
    with pytest.raises(IngestError, match="no assays for gene symbol 'EGFR'"):
        ingest_pubchem(cfg(), RawCache(tmp_path), f)


@pytest.mark.parametrize("exclude", [(), ("chembl ",), ("CHEMBL",)])
def test_chembl_deposits_are_always_dropped_whatever_the_config(tmp_path, exclude):
    c = cfg().model_copy(
        update={"pubchem": cfg().pubchem.model_copy(update={"exclude_depositors": exclude})}
    )
    chembl_cleared = DepositorTerms(
        depositors=(
            *load_depositor_terms().depositors,
            load_depositor_terms().depositors[0].model_copy(update={"name": "ChEMBL"}),
        )
    )
    report = ingest_pubchem(c, RawCache(tmp_path), fetcher(Server()), terms=chembl_cleared)
    assert report.rows == 3 and reasons(report)["chembl_deposited_aid"] == 1


def test_a_ledger_change_between_a_failed_run_and_its_resume_is_refused(tmp_path):
    server = Server()
    server.fail["/concise/JSON"] = [404]
    with pytest.raises(Exception, match="404"):
        ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(server))
    with pytest.raises(IngestError, match="depositor ledger"):
        ingest_pubchem(cfg(), RawCache(tmp_path), fetcher(Server()), terms=widened())
    report = ingest_pubchem(
        cfg(), RawCache(tmp_path), fetcher(Server()), terms=widened(), new_release=True
    )
    assert report.release == f"{RELEASE}.2"


def test_the_release_is_the_utc_date(tmp_path):
    late = "Sun, 15 Nov 2026 23:30:00 -0500"  # 04:30 UTC on 16 Nov

    def evening(request: httpx.Request) -> httpx.Response:
        resp = Server()(request)
        return httpx.Response(resp.status_code, content=resp.content, headers={"Date": late})

    f = Fetcher(transport=httpx.MockTransport(evening), sleep=lambda _s: None)
    assert ingest_pubchem(cfg(), RawCache(tmp_path), f).release == "2026-11-16"


def test_releases_order_by_their_numeric_suffix(tmp_path):
    cache = RawCache(tmp_path)
    for _ in range(11):
        ingest_pubchem(cfg(), cache, fetcher(Server()), new_release=True)
    assert read_pubchem(cfg(), cache).release == f"{RELEASE}.11"
    report = ingest_pubchem(cfg(), cache, fetcher(Server()))
    assert report.release == f"{RELEASE}.11" and report.requests_made == 0


@pytest.mark.parametrize("keep", ['"true"', "yes", "on", "1"])
def test_keep_must_be_a_real_boolean(tmp_path, keep):
    p = tmp_path / "terms.yaml"
    p.write_text(
        f"depositors:\n  - {{name: A, keep: {keep}, licence: x, attribution: y, "
        "evidence: [https://a], checked: 2026-11-15}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid"):
        load_depositor_terms(p)


def test_depositor_names_differing_only_by_case_are_refused(tmp_path):
    p = tmp_path / "terms.yaml"
    row = (
        "{{name: {n}, keep: {k}, licence: x, attribution: y, evidence: [https://a], "
        "checked: 2026-11-15}}"
    )
    p.write_text(
        "depositors:\n  - "
        + row.format(n="A", k="true")
        + "\n  - "
        + row.format(n="a", k="false")
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="listed twice"):
        load_depositor_terms(p)


def test_the_fetcher_returns_expected_statuses_instead_of_raising():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"Fault": {"Code": "PUGREST.NotFound"}}, headers={"Date": DATE}
        )

    f = Fetcher(transport=httpx.MockTransport(respond), sleep=lambda _s: None)
    got = f.get_json("https://example.invalid/x", {}, frozenset({404}))
    assert got.status == 404 and got.data["Fault"]["Code"] == "PUGREST.NotFound"
    assert f.requests_made == 1  # not retried
    with pytest.raises(Exception, match="404"):
        f.get_json("https://example.invalid/x", {})


# ---- verifier pass 2 findings ----


def test_a_transient_batch_not_found_is_re_asked_per_cid(tmp_path):
    server = Server()
    first = {"done": False}

    def flaky(request: httpx.Request) -> httpx.Response:
        if "/property/" in request.url.path and "," in request.url.path and not first["done"]:
            first["done"] = True
            server.log.append(request)
            fault = {"Fault": {"Code": "PUGREST.NotFound", "Message": "transient"}}
            return httpx.Response(404, json=fault, headers={"Date": DATE})
        return server(request)

    f = Fetcher(transport=httpx.MockTransport(flaky), sleep=lambda _s: None)
    cache = RawCache(tmp_path)
    report = ingest_pubchem(cfg(), cache, f)
    assert report.rows == 3  # one bad batch answer costs nothing: each CID was re-asked
    assert len(server.paths("/property/")) == 1 + 3
    assert read_pubchem(cfg(), cache).rows == read_pubchem(cfg(), cache).rows


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"Fault": {"Code": "PUGREST.NotFound"}}),  # a fault is only NotFound on a 404
        (404, {"Fault": "x"}),
        (404, {"Fault": None}),
        (404, {"Fault": []}),
        (404, ["Fault"]),
    ],
)
def test_odd_fault_bodies_fail_cleanly_and_are_not_cached(tmp_path, status, body):
    server = Server()

    def odd(request: httpx.Request) -> httpx.Response:
        if "/property/" in request.url.path:
            return httpx.Response(status, json=body, headers={"Date": DATE})
        return server(request)

    f = Fetcher(transport=httpx.MockTransport(odd), sleep=lambda _s: None)
    cache = RawCache(tmp_path)
    with pytest.raises(IngestError, match="unexpected PubChem response"):
        ingest_pubchem(cfg(), cache, f)
    assert not any("/property/" in r.url for r in cache.index("pubchem", RELEASE).values())


def test_a_folder_that_is_not_a_release_is_never_picked(tmp_path):
    import shutil

    cache = RawCache(tmp_path)
    ingest_pubchem(cfg(), cache, fetcher(Server()))
    shutil.copytree(tmp_path / "pubchem" / RELEASE, tmp_path / "pubchem" / "backup")
    shutil.copytree(tmp_path / "pubchem" / RELEASE, tmp_path / "pubchem" / "2026-11-14")
    assert read_pubchem(cfg(), cache).release == RELEASE  # the copy's cursor names RELEASE
    report = ingest_pubchem(cfg(), cache, fetcher(Server()))
    assert report.release == RELEASE and report.requests_made == 0
