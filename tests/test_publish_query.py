"""Network bounds and retry behavior of the authenticated derived-index writer."""
import io
import json
import urllib.error

import pytest
from publish_query import MAX_BYTES, QueryPublisher


def publisher():
    return QueryPublisher("https://data.example.test", "secret" * 8, "a" * 64, True)


def test_retries_the_exact_operation_without_exposing_credentials(monkeypatch):
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 30
        if len(requests) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(b'{"ok":true}')
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert publisher().send({"op": "finish", "results": 0}) == {"ok": True}
    assert len({request.data for request in requests}) == 1


def test_request_and_response_bounds(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversized input reached the network")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(ValueError, match="256 KiB"):
        publisher().send({"op": "record", "padding": "é" * MAX_BYTES})
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(b"x" * (MAX_BYTES + 1)))
    with pytest.raises(RuntimeError, match="oversized response"):
        publisher().send({"op": "inspect"})


def test_reconciliation_walks_bounded_pages_and_checks_both_totals(tmp_path):
    rows = [{"id": f"PALOMAR-2026-08-27-{n:06d}", "fingerprint": str(n), "repository": "same"} for n in range(31)]
    (tmp_path / "query-input.jsonl").write_text("".join(json.dumps({"op": "record", "record": row}) + "\n" for row in rows))
    p = publisher()
    calls = []
    def send(op):
        calls.append(op)
        if op["op"] == "inspect":
            return {"results": 31, "projects": 2, "release": p.release}
        selected = [row for row in rows if row["id"] > op.get("after", "")]
        return {"entries": selected[:25], "next": selected[24]["id"] if len(selected) > 25 else None}
    p.send = send
    assert p.reconcile(tmp_path) == ["query index project count differs"]
    assert sum(op["op"] == "export" for op in calls) == 2


@pytest.mark.parametrize("kind", ["disconnect", "short_read", "reset", "json", "rate_limit"])
def test_retries_lost_response_reads_and_rate_limits(monkeypatch, kind):
    import http.client
    failures = {
        "disconnect": http.client.RemoteDisconnected("closed"),
        "short_read": http.client.IncompleteRead(b"partial", 10),
        "reset": ConnectionResetError("reset"),
        "json": json.JSONDecodeError("truncated", "{", 1),
    }
    requests = []
    class BrokenRead(io.BytesIO):
        def read(self, *args):
            raise failures[kind]
    def open_request(request, timeout):
        requests.append(request.data)
        if len(requests) == 1:
            if kind == "rate_limit":
                raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, io.BytesIO())
            return BrokenRead()
        return io.BytesIO(b'{"ok":true}')
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert publisher().send({"op": "complete", "id": "PALOMAR-2026-08-27-000001", "chunks": 0}) == {"ok": True}
    assert len(requests) == 2 and requests[0] == requests[1]


def test_reports_only_a_known_error_code(monkeypatch):
    def open_request(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(b'{"error":"rebuild_required","message":"untrusted details"}'))
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    with pytest.raises(RuntimeError, match=r"HTTP 409 \(rebuild_required\)") as raised:
        publisher().send({"op": "begin"})
    assert "untrusted details" not in str(raised.value)
