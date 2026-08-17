import io

import public_source_availability


class _Response(io.BytesIO):
    def __init__(self, body: bytes, *, status: int = 200):
        super().__init__(body)
        self.headers = {"ETag": '"current"'}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_public_reads_identify_the_updater(monkeypatch):
    seen = []

    def open_request(request, *, timeout):
        seen.append((request, timeout))
        return _Response(b'{"schema_version": 1}\n')

    monkeypatch.setattr(public_source_availability.urllib.request, "urlopen", open_request)

    document, etag = public_source_availability._read("https://data.example/targets")

    assert document == {"schema_version": 1}
    assert etag == '"current"'
    assert seen[0][0].get_header("User-agent") == public_source_availability.USER_AGENT
    assert seen[0][1] == 30


def test_public_writes_identify_the_updater(monkeypatch):
    seen = []

    def open_request(request, *, timeout):
        seen.append((request, timeout))
        return _Response(b"", status=204)

    monkeypatch.setattr(public_source_availability.urllib.request, "urlopen", open_request)

    public_source_availability._put(
        "https://data.example/_operations/source-availability",
        "a" * 32,
        b"{}\n",
        '"current"',
    )

    request, timeout = seen[0]
    assert request.get_header("User-agent") == public_source_availability.USER_AGENT
    assert request.get_header("If-match") == '"current"'
    assert request.get_method() == "PUT"
    assert timeout == 30


def test_target_digest_uses_the_worker_protocol_key_order():
    # The private publisher sorts JSON object keys; protocol hashing must not
    # inherit that presentation order after parsing the target document.
    target = {
        "commit": "a" * 40,
        "fork_repository": "PalomarArchive/example",
        "source_repository": "example/source",
    }

    assert public_source_availability._digest([target]) == (
        "2701e52ebf5bf6effda86213cfb7da4faf67e3a55596abbfa510249ba98b42d6"
    )


def _refresh(monkeypatch, tmp_path, coverage, *, published):
    """Drive `main` with the probe replaced, and record whether it published."""
    targets = [{
        "source_repository": "example/project",
        "commit": "a" * 40,
        "fork_repository": "PalomarArchive/example--project--aaaaaaaaaaaa",
    }]

    def read(url):
        if url.endswith("source-availability-targets.json"):
            return {
                "schema_version": 1,
                "generated_at": "2026-08-16T00:00:00Z",
                "database_commit": "b" * 40,
                "targets": targets,
            }, None
        return {}, None

    def build(*_args, **_kwargs):
        return {
            "schema_version": 1,
            "generated_at": "2026-08-16T00:00:00Z",
            "coverage": {
                "observations_fresh": 1,
                "observations_total": 1,
                **coverage,
            },
            "repositories": [],
        }

    monkeypatch.setattr(public_source_availability, "_read", read)
    monkeypatch.setattr(public_source_availability, "build_manifest", build)
    monkeypatch.setattr(public_source_availability, "archive_is_degraded", lambda _m: False)
    monkeypatch.setattr(
        public_source_availability, "_put",
        lambda *args, **kwargs: published.append(args),
    )
    monkeypatch.setenv("PALOMAR_AVAILABILITY_UPDATE_TOKEN", "t" * 32)
    output = tmp_path / "manifest.json"
    return public_source_availability.main(["--output", str(output)]), output


def test_a_run_that_exhausted_its_budget_publishes_nothing(monkeypatch, tmp_path):
    """It does not know whether the sources it never reached are still there,
    and the endpoint cannot tell: it checks that the rows are the target set's,
    not that the run behind them finished."""
    published = []
    code, output = _refresh(
        monkeypatch,
        tmp_path,
        {"budget_exhausted": True, "queries_total": 2, "planned_refresh_cycle_hours": 6},
        published=published,
    )

    assert code == public_source_availability.INCOMPLETE_EXIT
    assert published == []
    # The artifact is still written, so the run that refuses leaves the
    # document it would have published to look at.
    assert output.is_file()


def test_a_cadence_the_run_cannot_keep_publishes_nothing(monkeypatch, tmp_path):
    """A manifest whose own coverage needs longer than the freshness window to
    come round again is promising something it cannot deliver."""
    published = []
    code, output = _refresh(
        monkeypatch,
        tmp_path,
        {"budget_exhausted": False, "queries_total": 4000, "planned_refresh_cycle_hours": 24},
        published=published,
    )

    assert code == public_source_availability.CAPACITY_EXIT
    assert published == []
    assert output.is_file()


def test_a_complete_run_within_capacity_publishes(monkeypatch, tmp_path):
    published = []
    code, _output = _refresh(
        monkeypatch,
        tmp_path,
        {"budget_exhausted": False, "queries_total": 58, "planned_refresh_cycle_hours": 6},
        published=published,
    )

    assert code == 0
    assert len(published) == 1
