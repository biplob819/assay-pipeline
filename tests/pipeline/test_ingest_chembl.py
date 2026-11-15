"""N1.2: ChEMBL ingest on recorded real responses, through a mock transport (no network)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import httpx
import pytest

from assay_pipeline.config import load_build_config
from assay_pipeline.pipeline.ingest.cache import CacheError, RawCache, request_key
from assay_pipeline.pipeline.ingest.chembl import REQUIRED_FIELDS, IngestError, ingest_chembl
from assay_pipeline.pipeline.ingest.http import USER_AGENT, Fetcher, FetchError
from assay_pipeline.schemas import RawResponse

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "chembl"
DATE = "Sun, 15 Nov 2026 13:24:00 GMT"


def cfg(page_size: int = 5):
    c = load_build_config()
    return c.model_copy(update={"chembl": c.chembl.model_copy(update={"page_size": page_size})})


class Server:
    """Serves the recorded fixture; can inject failures and changes per request."""

    def __init__(self) -> None:
        self.status = json.loads((FIX / "status.json").read_text(encoding="utf-8"))
        self.pages = {
            o: json.loads((FIX / f"activity_offset_{o}.json").read_text(encoding="utf-8"))
            for o in (0, 5, 10)
        }
        self.log: list[httpx.Request] = []
        self.fail: list[int | str] = []  # queued failures: an HTTP status or "transport"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        if self.fail:
            f = self.fail.pop(0)
            if f == "transport":
                raise httpx.ConnectError("planted", request=request)
            if f == "notjson":
                return httpx.Response(200, content=b"<html>busy</html>", headers={"Date": DATE})
            return httpx.Response(int(f), json={"error": "planted"}, headers={"Date": DATE})
        if request.url.path.endswith("/status.json"):
            return httpx.Response(200, json=self.status, headers={"Date": DATE})
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=self.pages[offset], headers={"Date": DATE})

    def activity_requests(self) -> list[int]:
        return [int(r.url.params["offset"]) for r in self.log if "activity" in r.url.path]


def fetcher(server: Server) -> Fetcher:
    return Fetcher(transport=httpx.MockTransport(server), sleep=lambda _s: None)


def all_records(cache: RawCache, release: str) -> list[dict]:
    out = []
    for row in cache.index("chembl", release).values():
        body = json.loads(cache.get("chembl", release, row.request_key))
        if "page_meta" in body:  # status.json also has an "activities" key (a count)
            out.extend(body["activities"])
    return sorted(out, key=lambda r: r["activity_id"])


def test_paging_caches_all_records_in_activity_id_order(tmp_path):
    server = Server()
    cache = RawCache(tmp_path)
    report = ingest_chembl(cfg(), cache, fetcher(server))
    assert (report.release, report.pages, report.records, report.complete) == (
        "ChEMBL_37",
        3,
        15,
        True,
    )
    assert server.activity_requests() == [0, 5, 10]
    params = server.log[1].url.params
    assert params["target_chembl_id"] == "CHEMBL203" and params["standard_type"] == "IC50"
    assert params["order_by"] == "activity_id" and params["limit"] == "5"
    ids = [r["activity_id"] for r in all_records(cache, "ChEMBL_37")]
    assert ids == sorted(ids) and len(ids) == 15


def test_every_cached_record_has_the_fields_later_stages_need(tmp_path):
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, fetcher(Server()))
    for r in all_records(cache, "ChEMBL_37"):
        assert set(REQUIRED_FIELDS) <= set(r)
    assert {"assay_description", "assay_variant_mutation", "src_id"} <= set(REQUIRED_FIELDS)


def test_a_record_for_another_target_stops_with_the_offset(tmp_path):
    server = Server()
    server.pages[5]["activities"][2]["target_chembl_id"] = "CHEMBL1824"
    with pytest.raises(IngestError, match=r"offset 5: record \d+ is CHEMBL1824"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_a_record_missing_a_field_stops_with_the_offset(tmp_path):
    server = Server()
    del server.pages[10]["activities"][0]["assay_description"]
    with pytest.raises(IngestError, match="offset 10: activity record lacks fields"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_release_is_read_first_and_bodies_are_indexed_gzip_mtime_zero(tmp_path):
    server = Server()
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, fetcher(server))
    assert server.log[0].url.path.endswith("/status.json")
    rows = cache.index("chembl", "ChEMBL_37")
    assert len(rows) == 4  # status + 3 pages
    for key, row in rows.items():
        assert isinstance(row, RawResponse) and row.release == "ChEMBL_37"
        gz = (tmp_path / "chembl" / "ChEMBL_37" / key[:2] / f"{key}.json.gz").read_bytes()
        assert gz[4:8] == b"\x00\x00\x00\x00"  # gzip mtime field
        assert row.fetched_at.isoformat() == "2026-11-15T13:24:00+00:00"


def test_a_tampered_body_is_detected(tmp_path):
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, fetcher(Server()))
    key = next(iter(cache.index("chembl", "ChEMBL_37")))
    path = tmp_path / "chembl" / "ChEMBL_37" / key[:2] / f"{key}.json.gz"
    path.write_bytes(gzip.compress(b'{"tampered": true}', mtime=0))
    with pytest.raises(CacheError, match="does not match its sha256"):
        cache.get("chembl", "ChEMBL_37", key)


def failing_at(server: Server, offset: str) -> Fetcher:
    """A fetcher whose network drops on one page, with no retries."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "activity" in request.url.path and request.url.params["offset"] == offset:
            raise httpx.ConnectError("network dropped", request=request)
        return server(request)

    return Fetcher(transport=httpx.MockTransport(handler), attempts=1, sleep=lambda _s: None)


def test_resume_after_interruption_refetches_nothing_cached(tmp_path):
    server = Server()
    cache = RawCache(tmp_path)
    with pytest.raises(FetchError, match="network dropped"):
        ingest_chembl(cfg(), cache, failing_at(server, "10"))
    cursor = cache.read_cursor("chembl", "ChEMBL_37")
    assert cursor is not None and cursor["offset"] == 10
    server.log.clear()
    report = ingest_chembl(cfg(), cache, fetcher(server))
    assert server.activity_requests() == [10]  # pages 0 and 5 came from the cache
    assert (report.records, report.cached_hits) == (15, 2)
    server.log.clear()
    again = ingest_chembl(cfg(), cache, fetcher(server))
    assert server.activity_requests() == [] and again.records == 15


def test_a_new_live_release_with_an_incomplete_cache_stops(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(server, "5"))
    server.status["chembl_db_version"] = "ChEMBL_38"
    with pytest.raises(IngestError, match="incomplete ChEMBL_37 ingest, but ChEMBL now serves"):
        ingest_chembl(cfg(), cache, fetcher(server))
    report = ingest_chembl(cfg(), cache, fetcher(server), new_release=True)
    assert report.release == "ChEMBL_38" and report.records == 15
    assert cache.releases("chembl") == ["ChEMBL_37", "ChEMBL_38"]


def test_total_count_changing_mid_run_stops(tmp_path):
    server = Server()
    server.pages[5]["page_meta"]["total_count"] = 16
    with pytest.raises(IngestError, match="offset 5: total_count changed from 15 to 16"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_a_complete_cache_makes_no_page_requests_and_writes_nothing(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    ingest_chembl(cfg(), cache, fetcher(server))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    server.log.clear()
    ingest_chembl(cfg(), cache, fetcher(server))
    assert server.activity_requests() == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_a_cache_built_for_another_target_is_refused(tmp_path):
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, fetcher(Server()))
    with pytest.raises(IngestError, match="use another --cache folder"):
        ingest_chembl(cfg(page_size=10), cache, fetcher(Server()))


@pytest.mark.parametrize("failure", ["transport", 429, 500, 503])
def test_transient_failures_are_retried_with_backoff(tmp_path, failure):
    server = Server()
    server.fail = [failure, failure]
    sleeps: list[float] = []
    ticks = iter(range(0, 10**6, 10))  # a clock that always advances: pacing never sleeps
    f = Fetcher(
        transport=httpx.MockTransport(server),
        sleep=sleeps.append,
        clock=lambda: float(next(ticks)),
    )
    report = ingest_chembl(cfg(), RawCache(tmp_path), f)
    assert report.records == 15
    assert len(sleeps) == 2  # exactly the two backoffs, nothing from pacing
    assert 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0  # jittered, doubling caps


def test_retries_give_up_after_six_attempts_with_nothing_written(tmp_path):
    server = Server()
    server.fail = [503] * 6
    with pytest.raises(FetchError, match="gave up after 6 attempts"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))
    assert len(server.log) == 6
    assert not list(tmp_path.rglob("*.gz"))


def test_backoff_is_capped(tmp_path):
    f = Fetcher(max_delay=4.0)
    assert all(f._backoff(n) <= 4.0 for n in range(20))
    assert all(f._backoff(n) >= 0.25 for n in range(20))


@pytest.mark.parametrize(
    ("failure", "needle"), [(404, "HTTP 404, not retried"), ("notjson", "not JSON")]
)
def test_other_failures_are_never_retried(tmp_path, failure, needle):
    server = Server()
    server.fail = [failure]
    with pytest.raises(FetchError, match=needle):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))
    assert len(server.log) == 1


def test_user_agent_names_the_project_only(tmp_path):
    server = Server()
    ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))
    agents = {r.headers["User-Agent"] for r in server.log}
    assert agents == {USER_AGENT}
    assert USER_AGENT.startswith("assay-pipeline/") and "@" not in USER_AGENT


@pytest.mark.rebuild
def test_two_ingests_into_empty_caches_are_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    ingest_chembl(cfg(), RawCache(a), fetcher(Server()))
    ingest_chembl(cfg(), RawCache(b), fetcher(Server()))
    files_a = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert files_a == files_b and len(files_a) == 6  # 4 bodies, index, cursor


def test_request_key_ignores_param_order():
    k1 = request_key("GET", "https://x/a", {"b": 1, "a": 2})
    assert k1 == request_key("get", "https://x/a", {"a": 2, "b": 1})
    assert k1 != request_key("GET", "https://x/a", {"a": 2, "b": 3})


def test_fixture_carries_the_licence_attribution():
    readme = (FIX / "README.md").read_text(encoding="utf-8")
    assert "CC BY-SA 3.0" in readme and "EMBL-EBI" in readme


def config_file(tmp_path: Path, page_size: int = 5) -> Path:
    from importlib import resources

    text = (resources.files("assay_pipeline") / "defaults" / "build.yaml").read_text(
        encoding="utf-8"
    )
    p = tmp_path / "build.yaml"
    p.write_text(text.replace("page_size: 1000", f"page_size: {page_size}"), encoding="utf-8")
    return p


def test_cli_ingest_reports_json(tmp_path, monkeypatch, capsys):
    from assay_pipeline import cli

    server = Server()
    monkeypatch.setattr(cli, "default_fetcher", lambda _rps: fetcher(server))
    args = ["ingest", "chembl", "--config", str(config_file(tmp_path)), "--json"]
    code = cli.main([*args, "--cache", str(tmp_path / "cache")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out == {
        "ok": True,
        "source": "chembl",
        "release": "ChEMBL_37",
        "pages": 3,
        "records": 15,
        "requests_made": 4,
        "cached_hits": 0,
        "complete": True,
    }


def test_cli_ingest_errors_exit_2_with_the_reason(tmp_path, monkeypatch, capsys):
    from assay_pipeline import cli

    server = Server()
    server.pages[5]["page_meta"]["total_count"] = 99
    monkeypatch.setattr(cli, "default_fetcher", lambda _rps: fetcher(server))
    args = ["ingest", "chembl", "--config", str(config_file(tmp_path)), "--json"]
    assert cli.main([*args, "--cache", str(tmp_path / "cache")]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "total_count changed" in out["error"]


GOLDEN = FIX / "expected_cache.sha256"


def cache_digest(root: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        data = p.read_bytes()
        # hash bodies decompressed: deflate bytes may differ between zlib builds
        h.update(gzip.decompress(data) if p.suffix == ".gz" else data)
    return h.hexdigest()


@pytest.mark.golden
def test_golden_ingest_reproduces_the_recorded_cache(tmp_path):
    """The recorded fixture always ingests to the same bytes; a change here is a format change."""
    ingest_chembl(cfg(), RawCache(tmp_path), fetcher(Server()))
    assert cache_digest(tmp_path) == GOLDEN.read_text(encoding="utf-8").strip()


# ---- verifier pass 1 findings ----


def test_a_bad_page_is_never_cached_so_a_healthy_rerun_succeeds(tmp_path):
    server = Server()
    good = json.loads(json.dumps(server.pages[5]))
    server.pages[5]["activities"][0]["target_chembl_id"] = "CHEMBL1824"
    cache = RawCache(tmp_path)
    with pytest.raises(IngestError, match="offset 5"):
        ingest_chembl(cfg(), cache, fetcher(server))
    assert len(cache.index("chembl", "ChEMBL_37")) == 2  # status + page 0 only
    server.pages[5] = good
    assert ingest_chembl(cfg(), cache, fetcher(server)).records == 15


def test_total_count_error_gives_advice_that_works(tmp_path):
    server = Server()
    server.pages[5]["page_meta"]["total_count"] = 16
    cache = RawCache(tmp_path)
    with pytest.raises(IngestError, match=r"rerun later \(nothing bad was cached\)"):
        ingest_chembl(cfg(), cache, fetcher(server))
    assert len(cache.index("chembl", "ChEMBL_37")) == 2  # status + page 0; the bad page is not
    server.pages[5]["page_meta"]["total_count"] = 15
    assert ingest_chembl(cfg(), cache, fetcher(server)).records == 15


def test_an_older_release_folder_without_a_cursor_counts_as_incomplete(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(server, "0"))
    assert cache.read_cursor("chembl", "ChEMBL_37") is None
    server.status["chembl_db_version"] = "ChEMBL_38"
    with pytest.raises(IngestError, match="incomplete ChEMBL_37"):
        ingest_chembl(cfg(), cache, fetcher(server))


def test_new_release_abandons_the_old_one_and_later_runs_need_no_flag(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(server, "5"))
    server.status["chembl_db_version"] = "ChEMBL_38"
    ingest_chembl(cfg(), cache, fetcher(server), new_release=True)
    old = cache.read_cursor("chembl", "ChEMBL_37")
    assert old is not None and old["abandoned"] and old["superseded_by"] == "ChEMBL_38"
    assert ingest_chembl(cfg(), cache, fetcher(server)).release == "ChEMBL_38"


@pytest.mark.parametrize("status", [501, 505, 520, 524])
def test_every_5xx_is_retried(tmp_path, status):
    server = Server()
    server.fail = [status]
    assert ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server)).records == 15


def test_retry_after_is_honoured_up_to_the_cap(tmp_path):
    server = Server()
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not sleeps and "status" in request.url.path:
            sleeps.append(-1)  # marker: first call
            return httpx.Response(429, headers={"Retry-After": "7", "Date": DATE})
        return server(request)

    waits: list[float] = []
    f = Fetcher(transport=httpx.MockTransport(handler), sleep=waits.append, max_delay=5.0)
    ingest_chembl(cfg(), RawCache(tmp_path), f)
    assert 5.0 in waits  # 7 s requested, capped at max_delay


@pytest.mark.parametrize(
    "exc",
    [httpx.TooManyRedirects("loop"), httpx.DecodingError("bad gzip")],
)
def test_non_transient_http_errors_become_fetch_errors_naming_the_offset(tmp_path, exc):
    server = Server()

    def handler(request: httpx.Request) -> httpx.Response:
        if "activity" in request.url.path:
            raise exc
        return server(request)

    f = Fetcher(transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    with pytest.raises(FetchError, match="'offset': 0"):
        ingest_chembl(cfg(), RawCache(tmp_path), f)


def test_corrupt_cache_files_are_clean_errors_with_exit_2(tmp_path, monkeypatch, capsys):
    from assay_pipeline import cli

    cache_dir = tmp_path / "cache"
    ingest_chembl(cfg(), RawCache(cache_dir), fetcher(Server()))
    release = cache_dir / "chembl" / "ChEMBL_37"
    (release / "cursor.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "default_fetcher", lambda _rps: fetcher(Server()))
    args = ["ingest", "chembl", "--config", str(config_file(tmp_path)), "--cache", str(cache_dir)]
    assert cli.main([*args, "--json"]) == 2
    assert "corrupt cursor" in json.loads(capsys.readouterr().out)["error"]
    (release / "cursor.json").unlink()
    gz = next(release.rglob("*.json.gz"))
    gz.write_bytes(b"not gzip at all")
    with pytest.raises(CacheError, match="body unreadable"):
        RawCache(cache_dir).get("chembl", "ChEMBL_37", gz.name.removesuffix(".json.gz"))


@pytest.mark.parametrize(
    "name", ["C:evil", "CON", "NUL.x", "con.1", "x..", "../x", ".hidden", "a b", "", "x" * 80]
)
def test_release_names_are_allow_listed(tmp_path, name):
    with pytest.raises(CacheError, match="unsafe release name"):
        RawCache(tmp_path).release_dir("chembl", name)


@pytest.mark.parametrize("value", [None, "", 37, "C:evil"])
def test_an_unusable_release_from_chembl_stops_the_ingest(tmp_path, value):
    server = Server()
    server.status["chembl_db_version"] = value
    with pytest.raises(IngestError, match=r"chembl_db_version|unusable release"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_stray_folders_in_the_cache_are_ignored(tmp_path):
    (tmp_path / "chembl" / ".DS_Store_dir").mkdir(parents=True)
    (tmp_path / "chembl" / "not a release").mkdir()
    assert ingest_chembl(cfg(), RawCache(tmp_path), fetcher(Server())).records == 15


def test_a_second_writer_is_refused_and_the_lock_is_released(tmp_path):
    cache = RawCache(tmp_path)
    lock = cache.lock("chembl")
    with pytest.raises(CacheError, match="another ingest"):
        ingest_chembl(cfg(), cache, fetcher(Server()))
    lock.unlink()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(Server(), "5"))
    assert not (tmp_path / "chembl" / ".lock").exists()  # released after a failure too
    assert ingest_chembl(cfg(), cache, fetcher(Server())).records == 15


def test_overlapping_or_reordered_pages_are_detected(tmp_path):
    server = Server()
    server.pages[5] = json.loads(json.dumps(server.pages[0]))
    server.pages[5]["page_meta"]["offset"] = 5
    with pytest.raises(IngestError, match="does not follow"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_fetched_at_must_come_from_the_date_header(tmp_path):
    server = Server()

    def no_date(request: httpx.Request) -> httpx.Response:
        r = server(request)
        return httpx.Response(r.status_code, content=r.content)

    f = Fetcher(transport=httpx.MockTransport(no_date), sleep=lambda _s: None)
    with pytest.raises(CacheError, match="no Date header"):
        ingest_chembl(cfg(), RawCache(tmp_path), f)


def test_a_minus_zero_date_header_is_utc(tmp_path):
    server = Server()

    def minus_zero(request: httpx.Request) -> httpx.Response:
        r = server(request)
        return httpx.Response(
            200, content=r.content, headers={"Date": "Sun, 15 Nov 2026 13:24:00 -0000"}
        )

    f = Fetcher(transport=httpx.MockTransport(minus_zero), sleep=lambda _s: None)
    cache = RawCache(tmp_path)
    ingest_chembl(cfg(), cache, f)
    row = next(iter(cache.index("chembl", "ChEMBL_37").values()))
    assert row.fetched_at.isoformat() == "2026-11-15T13:24:00+00:00"


def test_sockets_are_really_blocked_in_pipeline_tests():
    import socket

    with pytest.raises(RuntimeError, match="must not use the network"):
        socket.create_connection(("www.ebi.ac.uk", 443), timeout=1)


def test_new_release_does_not_abandon_when_the_new_cache_is_refused(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(server, "5"))
    server.status["chembl_db_version"] = "ChEMBL_38"
    other_config = {
        "release": "ChEMBL_38",
        "target": "CHEMBL1",
        "activity_type": "IC50",
        "page_size": 5,
        "offset": 0,
        "total_count": None,
        "complete": False,
    }
    cache.write_cursor("chembl", "ChEMBL_38", other_config)
    with pytest.raises(IngestError, match="use another --cache folder"):
        ingest_chembl(cfg(), cache, fetcher(server), new_release=True)
    old = cache.read_cursor("chembl", "ChEMBL_37")
    assert old is not None and not old.get("abandoned")


def test_an_abandoned_release_that_comes_back_resumes_cleanly(tmp_path):
    cache = RawCache(tmp_path)
    server = Server()
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(server, "0"))  # ChEMBL_37 never cached a page
    server.status["chembl_db_version"] = "ChEMBL_38"
    ingest_chembl(cfg(), cache, fetcher(server), new_release=True)
    server.status["chembl_db_version"] = "ChEMBL_37"  # the old release is served again
    report = ingest_chembl(cfg(), cache, fetcher(server))
    assert report.release == "ChEMBL_37" and report.records == 15
    cur = cache.read_cursor("chembl", "ChEMBL_37")
    assert cur is not None and not cur.get("abandoned")


def test_unfollowed_redirects_and_other_non_2xx_are_errors(tmp_path):
    server = Server()
    server.fail = [301]
    with pytest.raises(FetchError, match="HTTP 301, not retried"):
        ingest_chembl(cfg(), RawCache(tmp_path), fetcher(server))


def test_a_cursor_without_config_over_cached_pages_is_refused(tmp_path):
    cache = RawCache(tmp_path)
    with pytest.raises(FetchError):
        ingest_chembl(cfg(), cache, failing_at(Server(), "5"))  # page 0 is cached
    cache.write_cursor("chembl", "ChEMBL_37", {"release": "ChEMBL_37", "complete": False})
    with pytest.raises(IngestError, match="holds cached pages but its cursor names no config"):
        ingest_chembl(cfg(), cache, fetcher(Server()))
