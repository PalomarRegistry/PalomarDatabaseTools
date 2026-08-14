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
