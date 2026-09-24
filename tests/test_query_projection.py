"""The listing bound must never become a bound on what can be found."""
import copy
import json

from conftest import SAMPLE_ENTRY_DATA
from query_projection import CHUNK_BYTES, SUMMARY_BYTES, encoded, search_chunks, summary, write_full_query


def test_abbreviation_does_not_limit_search():
    entry = copy.deepcopy(SAMPLE_ENTRY_DATA)
    entry["authors"] = [{"name": "A" * 2000} for _ in range(100)] + [{"name": "LateAuthor"}]
    entry["formalization"]["theorem_names"] = ["Many." + "x" * 2000] * 100 + ["LastTheorem"]
    result = summary(entry, 1)
    assert result["abbreviated"]
    assert len(encoded(result).encode()) <= SUMMARY_BYTES
    chunks = list(search_chunks(entry))
    assert all(len(chunk.encode()) <= CHUNK_BYTES for chunk in chunks)
    assert {"lateauthor", "lasttheorem"} <= set(" ".join(chunks).split())


def test_complete_large_search_text_is_chunked():
    entry = copy.deepcopy(SAMPLE_ENTRY_DATA)
    entry["authors"] = [{"name": f"person{n}"} for n in range(30_000)]
    chunks = list(search_chunks(entry))
    assert len(chunks) > 1
    assert "person29999" in chunks[-1].split()
    assert all(len(chunk.encode()) <= CHUNK_BYTES for chunk in chunks)


def test_display_never_truncates_a_source_url():
    entry = copy.deepcopy(SAMPLE_ENTRY_DATA)
    entry["source"]["repository"] = "owner/" + "x" * 30000
    for mapping in entry["preservation"]["repositories"]:
        if mapping["commit"] == entry["source"]["commit"]:
            mapping["source_repository"] = entry["source"]["repository"]
    result = summary(entry, 1)
    assert result["source_omitted"]
    assert result["source"] is None
    assert len(encoded(result).encode()) <= SUMMARY_BYTES


def test_full_input_has_only_current_versions_and_bounded_lines(tmp_path):
    first = copy.deepcopy(SAMPLE_ENTRY_DATA)
    first["version"] = 1
    second = copy.deepcopy(first)
    second["version"] = 2
    second["registry_correction"] = {"based_on": {"version": 1}}
    write_full_query(tmp_path, [first, second])
    lines = (tmp_path / "query-input.jsonl").read_bytes().splitlines()
    assert all(len(line) < 256 * 1024 for line in lines)
    operations = [json.loads(line) for line in lines]
    headers = [op["record"] for op in operations if op["op"] == "record"]
    assert len(headers) == 1
    display = json.loads(headers[0]["summary"])
    assert display["version"] == display["versions"] == 2
    assert display["preview"]["version"] == 1


def test_stager_never_publishes_the_query_stream(repo, tmp_path):
    import stage_public
    from release_delta import parse
    output = tmp_path / "staged"
    stage_public.stage_public(repo.path, output)
    delta = parse((output / "release-delta.json").read_bytes())
    assert (output / "query-input.jsonl").is_file()
    paths = [row["path"] for key in ("additions", "stable", "aggregates") for row in delta[key]]
    assert "query-input.jsonl" not in paths
    assert not any(path.startswith("search/") for path in paths)


def test_tokenizer_contract():
    from pathlib import Path
    from search_tokens import STOPWORDS, tokens
    fixture = json.loads((Path(__file__).parent / "fixtures/query-tokens.json").read_text())
    assert sorted(STOPWORDS) == fixture["stopwords"]
    for sample in fixture["cases"]:
        assert list(dict.fromkeys(tokens(sample["text"]))) == sample["words"]
