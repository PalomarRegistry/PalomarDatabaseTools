"""Searching the registry without a document that knows every word in it.

A term's postings are a sequence of sealed pages and a head of six numbers, so
publishing a result appends to one page per word it carries and rewrites one
head. Nothing lists the terms, because a list of terms grows with the
vocabulary, the vocabulary grows with the registry, and rewriting it once per
accepted result is the super-linear cost every other surface here has already
had removed.

The tests below measure that claim rather than restating it, and the
adversarial ones construct the distributions that would break it: a word in
every result, a word in one, a record made entirely of words that are dropped,
and a record whose text would be dangerous in a path.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil

import build_search
import pytest
import release_delta
import stage_public
from build_search import (
    MAX_TERMS_PER_RECORD,
    PAGE_SIZE,
    STOPWORDS,
    STOPWORDS_PATH,
    head_path,
    page_path,
    terms_of,
    tokens,
)
from stage_public import plan

SEARCH_PATH_RE = re.compile(r"search/t/[a-z0-9]{2,32}/(?:head|[0-9]{1,4})\.json")


def _document(site, relative):
    return json.loads((site / relative).read_text())


def _staged(site):
    """Every postings object a run produced, and nothing else under `search/`."""
    return sorted(
        path.relative_to(site).as_posix()
        for path in site.rglob("*.json")
        if path.is_file() and path.relative_to(site).as_posix().startswith("search/t/")
    )


def _terms_staged(site):
    root = site / "search" / "t"
    return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


def _postings(site, term):
    """Every result under one word, read the way a client reads it."""
    head = _document(site, head_path(term))
    return [
        posting
        for number in range(head["pages"])
        for posting in _document(site, page_path(term, number))["postings"]
    ]


def _answers(site):
    """Which results each word finds, which is all a reader can observe."""
    found = {term: sorted(_postings(site, term)) for term in _terms_staged(site)}
    return {term: postings for term, postings in found.items() if postings}


def _stage(root, site, **arguments):
    if arguments.get("previous") is not None:
        arguments["previous"] = release_delta.base_of(arguments["previous"])
    stage_public.stage_public(root, site, **arguments)
    return release_delta.parse((site / "release-delta.json").read_bytes())


def _rewritten(previous, delta, prefix):
    """Which objects under a prefix changed bytes, and by how many bytes.

    This measures payload growth, not Class A writes: the publisher rewrites
    every stable object the release stages, including the unchanged fixed
    stopword list. Counting changed bytes separately catches a page that
    quietly grew with the registry rather than charging a constant document's
    identical bytes as payload growth.
    """
    prior = {row["path"]: row["sha256"] for row in previous["stable"]}
    changed = [
        row
        for row in delta["stable"]
        if row["path"].startswith(prefix) and prior.get(row["path"]) != row["sha256"]
    ]
    return sorted(row["path"] for row in changed), sum(row["bytes"] for row in changed)


def _entries_read(monkeypatch, root):
    """The entry files a staging run opens, which is what must not grow."""
    opened: list[str] = []
    original = stage_public._load_json

    def counting(path):
        if path.parent == root / "entries":
            opened.append(path.name)
        return original(path)

    monkeypatch.setattr(stage_public, "_load_json", counting)
    return opened


def _take_down(database, identifier, version):
    database.write_json("takedowns.json", {
        "schema_version": 1,
        "takedowns": [{
            "id": identifier, "version": version,
            "taken_down_at": "2026-08-06T12:00:00Z",
            "authorized_by_login": "avigad", "authorization_issue": 101, "reason": "a private reason",
        }],
    })


def test_a_result_is_found_by_the_words_of_its_title_and_abstract(db, tmp_path):
    db.add_entry(
        "PALOMAR-2026-07-29-000002",
        1,
        title="Quasicoherent sheaves on a projective scheme",
        abstract="A finiteness theorem for coherent cohomology.",
    )
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    assert _postings(site, "quasicoherent") == ["PALOMAR-2026-07-29-000002-v1"]
    assert _postings(site, "cohomology") == ["PALOMAR-2026-07-29-000002-v1"]


def test_an_author_is_a_word_and_a_classification_code_is_not(db, tmp_path):
    """A name is the question readers arrive with and is already the most
    public part of a record. A code is not a word: `subjects/` and the category
    feeds answer it exactly, and tokenizing `math.NT` would only file every
    result in the registry under `math`."""
    db.add_entry(
        "PALOMAR-2026-07-29-000002",
        1,
        authors=[{"name": "Paul Erdős"}],
        classification={"arxiv": ["math.NT"], "msc2020": ["11N13"]},
    )
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    assert (site / head_path("erdos")).is_file()
    assert not (site / head_path("math")).exists()
    assert not (site / head_path("11n13")).exists()


def test_accents_are_folded_rather_than_split_on():
    """Erdős is one term and not `erd` and `s`, because `erdos` is what
    somebody types. The browser folds identically, and a query folded any other
    way is a request for a term that was never written."""
    assert tokens("Erdős–Kähler") == ["erdos", "kahler"]
    assert tokens("PoincarÉ") == ["poincare"]


def test_nothing_is_stemmed():
    """`ring` and `rings` are different questions in a registry of
    mathematics, and a stemmer that conflated them would make the commonest
    words here commoner still."""
    entry = {"title": "Rings and a ring", "abstract": "Fields and a field.", "authors": []}
    assert {"ring", "rings", "field", "fields"} <= terms_of(entry)


def test_nothing_published_names_more_than_one_term(db, tmp_path):
    """The property the whole shape exists for. A document listing the terms
    would be rewritten on every publication and would grow with the vocabulary,
    which grows with the registry: that is the O(S) per accepted result this
    design refuses to pay, and it is the obvious thing to add back."""
    for serial in range(5):
        db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    staged = _staged(site)
    assert staged, "nothing was indexed at all"
    assert all(SEARCH_PATH_RE.fullmatch(path) for path in staged)
    for path in staged:
        term = path.split("/")[2]
        document = _document(site, path)
        assert document["term"] == term, f"{path} names a term other than its own directory"
    # One published document names more than one word, and it is the only one
    # that may: the stopword list is a fixed editorial choice, so it is the
    # same size at a hundred thousand results as it is here.
    under_search = {
        path.relative_to(site).as_posix()
        for path in (site / "search").rglob("*")
        if path.is_file()
    }
    assert sorted(under_search - set(staged)) == [STOPWORDS_PATH]


def test_the_published_stopword_list_is_the_one_the_indexer_dropped(db, tmp_path):
    """One source of truth. A second copy in the browser would drift from this
    one across two independently deployed repositories, and a reader whose copy
    disagreed would ask for a word that was never written and be told, in a way
    they could not diagnose, that nothing carries it."""
    newcomer = db.next_identifier()
    db.add_entry(newcomer, 1, title="The ring of integers")
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    published = _document(site, STOPWORDS_PATH)
    assert set(published["stopwords"]) == set(STOPWORDS)
    assert "the" in published["stopwords"]
    assert not (site / head_path("the")).exists()
    assert _postings(site, "ring") == [f"{newcomer}-v1"]


def test_a_page_is_sealed_at_its_size_and_the_head_says_how_many_there_are(
    db, tmp_path, monkeypatch
):
    monkeypatch.setattr(build_search, "PAGE_SIZE", 4)
    for serial in range(2, 12):
        db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    head = _document(site, head_path("palomar"))
    assert head == {
        "schema_version": 1,
        "term": "palomar",
        "page_size": 4,
        "pages": 3,
        "results": 11,
    }
    lengths = [
        len(_document(site, page_path("palomar", number))["postings"])
        for number in range(head["pages"])
    ]
    assert lengths == [4, 4, 3], "a page was not sealed at its size"
    assert _postings(site, "palomar") == sorted(_postings(site, "palomar"))


def test_indexing_one_result_costs_the_same_whatever_the_registry_holds(
    repo, tmp_path, monkeypatch
):
    """The growth class, and the reason for every other decision in this file.

    Every record here carries the same words, which is the distribution that
    would expose anything growing with the registry: the word being appended to
    is a word every result already has.

    Thirteen and twenty-one, with pages of four, so that the record being added
    lands at the same offset in its page in both runs. The open page is what a
    publication rewrites, so comparing two runs at different offsets would
    compare two different amounts of page rather than two different registries.
    Measured as changed payload plus the entry files the run opens. The
    publisher writes every staged stable object, including the fixed stopword
    document; the digest comparison here isolates whether any object's bytes
    quietly grew with the registry. A design that reread the registry to append
    to it would write exactly as little and cost exactly as much. The page
    *numbers* do grow, since a longer sequence has more pages; what must not grow
    is how many of them one publication touches and how much of them it writes.
    """
    monkeypatch.setattr(build_search, "PAGE_SIZE", 4)
    paths, written, reads = {}, {}, {}
    for size in (13, 21):
        for serial in range(1, size):
            repo.add_entry(f"PALOMAR-2026-07-28-{serial:06d}", 1)
        repo.commit(f"a registry of {size}")
        previous = _stage(repo.path, tmp_path / f"base{size}")
        served = tmp_path / f"served{size}"
        shutil.copytree(tmp_path / f"base{size}", served)

        newcomer = repo.next_identifier()
        repo.add_entry(newcomer, 1)
        repo.commit("one more")
        opened = _entries_read(monkeypatch, repo.path)
        delta = _stage(
            repo.path, tmp_path / f"next{size}", previous=previous, prior=served
        )
        changed, written[size] = _rewritten(previous, delta, "search/")
        paths[size] = sorted(path.split("/")[2] for path in changed)
        reads[size] = list(opened)

        repo.remove(f"entries/{newcomer}-v1.json")
        repo.reindex()

    assert reads[13] == reads[21] == [f"{newcomer}-v1.json"]
    assert paths[13] == paths[21], "a different set of words was touched"
    assert written[13] == written[21], written


def test_a_word_carried_by_every_result_still_costs_one_page_and_one_head(
    repo, tmp_path, monkeypatch
):
    """The open page is bounded by its size, so the worst word in the registry
    costs a fixed amount to add to. The registry's own name is that word here:
    every identifier begins with it."""
    monkeypatch.setattr(build_search, "PAGE_SIZE", 4)
    for serial in range(1, 40):
        repo.add_entry(f"PALOMAR-2026-07-28-{serial:06d}", 1)
    repo.commit("a registry")
    previous = _stage(repo.path, tmp_path / "base")
    served = tmp_path / "served"
    shutil.copytree(tmp_path / "base", served)

    newcomer = repo.next_identifier()
    repo.add_entry(newcomer, 1)
    repo.commit("one more")
    site = tmp_path / "next"
    delta = _stage(repo.path, site, previous=previous, prior=served)
    changed, _bytes = _rewritten(previous, delta, "search/t/palomar/")

    assert changed == ["search/t/palomar/10.json", "search/t/palomar/head.json"]
    assert len(_document(site, "search/t/palomar/10.json")["postings"]) <= 4


def test_a_word_carried_by_one_result_is_a_page_of_one(db, tmp_path):
    for serial in range(2, 7):
        db.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    newcomer = db.next_identifier()
    db.add_entry(
        newcomer, 1, title="A hypergraph regularity lemma", abstract="One of a kind."
    )
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    assert _document(site, head_path("hypergraph")) == {
        "schema_version": 1,
        "term": "hypergraph",
        "page_size": PAGE_SIZE,
        "pages": 1,
        "results": 1,
    }
    assert _postings(site, "hypergraph") == [f"{newcomer}-v1"]


def test_a_record_made_entirely_of_stopwords_is_indexed_under_nothing(db, tmp_path):
    """It is not an error and it is not a term of its own: the record is still
    published, still browsable and still under its classification codes, and
    there is simply no word to find it by."""
    newcomer = db.next_identifier()
    db.add_entry(
        newcomer,
        1,
        title="Of the and",
        abstract="It is to be, and it has been.",
        authors=[{"name": "A"}],
    )
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    everything = {posting for term in _terms_staged(site) for posting in _postings(site, term)}
    assert f"{newcomer}-v1" not in everything
    assert (site / f"entries/{newcomer}-v1.json").is_file()


def test_text_that_would_be_unsafe_in_a_path_never_reaches_one(db, tmp_path):
    """Dropped rather than escaped. An encoded term would put record-controlled
    bytes in a served key and would make building that key a piece of shared
    logic the browser could get wrong; a term that is only ever
    `[a-z0-9]{2,32}` is a regular expression on both sides."""
    newcomer = db.next_identifier()
    db.add_entry(
        newcomer,
        1,
        title="../../etc/passwd %2e%2e C:\\Windows <script>",
        abstract="A\u202egnirts\u202c with \u2028separators\u2029 and \ud83d\ude00 emoji.",
    )
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    for path in _staged(site):
        assert SEARCH_PATH_RE.fullmatch(path), path
    # The words survive; the punctuation that made them dangerous does not, and
    # every directory under `search/t/` is a word rather than a fragment of a
    # path someone wrote into a title.
    assert {"passwd", "etc", "windows", "script"} <= set(_terms_staged(site))
    assert all(re.fullmatch(r"[a-z0-9]{2,32}", term) for term in _terms_staged(site))


def test_a_very_long_abstract_costs_what_it_carries_and_no_more(db, tmp_path):
    """Bounded by the record, which is what the schema is for: ten thousand
    characters of abstract and three hundred of title. A publication pays two
    objects per distinct word, so a schema that relaxed this bound would
    quietly turn one submission into an unbounded publication."""
    words = " ".join(f"w{number:04d}" for number in range(1600))
    db.add_entry(db.next_identifier(), 1, abstract=words[:10000])
    site = tmp_path / "release"
    stage_public.stage_public(db.path, site)

    staged = _staged(site)
    assert len(staged) == 2 * len(_terms_staged(site))
    assert 1500 < len(_terms_staged(site)) < MAX_TERMS_PER_RECORD


def test_a_record_with_more_terms_than_a_record_can_have_stops_the_publication(
    db, tmp_path, monkeypatch
):
    """Refused rather than truncated. A record indexed under some of its words
    is findable by some of them, and nothing would say which."""
    monkeypatch.setattr(build_search, "MAX_TERMS_PER_RECORD", 10)
    db.add_entry(
        db.next_identifier(),
        1,
        abstract=" ".join(f"w{number:04d}" for number in range(50)),
    )

    with pytest.raises(ValueError, match="distinct terms"):
        stage_public.stage_public(db.path, tmp_path / "release")


# Where the pages a release appends to come from, now that none of them are
# kept in the database this is published from.


def test_the_index_is_not_kept_in_the_database_it_is_published_from(
    repo, tmp_path_factory
):
    """Committing it would put about fifty kilobytes of new blobs into the
    ledger per accepted result, and git keeps every version of every changed
    page: five gigabytes of history at a hundred thousand results, which is
    unrecoverable once it is there."""
    repo.add_entry("PALOMAR-2026-07-29-000002", 1)
    repo.commit("register")
    before = repo.git("status", "--porcelain")

    stage_public.stage_public(repo.path, tmp_path_factory.mktemp("staged") / "release")

    assert repo.git("status", "--porcelain") == before
    assert not (repo.path / "search").exists()


def test_the_plan_names_a_head_before_it_can_name_the_page_that_head_points_at(served):
    """The two passes, and why there are two.

    Every other document a release patches is named from a result's identifier
    and its codes. The number of a word's open postings page is not: it comes
    from how many postings that word already has, which is a fact about the
    registry, and the only document that states it is the word's head. So the
    first pass names the heads, the credentialed step brings them back, and the
    second names the pages they point at. A third pass has to add nothing, or
    the fetching would not settle.
    """
    for serial in range(2, 8):
        served.database.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1)
    served.database.commit("one more")
    ready = plan(served.database.path.resolve(), release_delta.base_of(served.delta))

    first = ready.prior_paths()
    words = terms_of(served.database.read_json(f"entries/{newcomer}-v1.json"))
    assert [path for path in first if SEARCH_PATH_RE.fullmatch(path)] == sorted(
        head_path(word) for word in words
    ), "it named a head for a word the release does not touch"

    prior = served.root / "handed"
    _fetch(served.path, prior, first)
    second = ready.prior_paths(prior)
    assert page_path("palomar", 0) in second
    assert head_path("palomar") not in second, "it asked again for what it was given"

    # A third pass asks for nothing the first two did not, so the fetching
    # settles. What it does still name is what the bucket does not hold: a word
    # nothing has carried before has neither a head nor a page, and which of
    # those two an absence means is the stager's decision, not this step's.
    _fetch(served.path, prior, second)
    assert not set(ready.prior_paths(prior)) - set(first) - set(second)
    assert not any((prior / relative).is_file() for relative in ready.prior_paths(prior))


def _fetch(bucket, prior, names):
    """What the credentialed step does: copy exactly these, skipping absences."""
    for relative in names:
        source = bucket / relative
        if source.is_file():
            (prior / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, prior / relative)


def test_both_passes_of_the_plan_run_the_way_the_workflow_runs_them(served, tmp_path):
    """The contract between the workflow steps, which is two files of paths.

    Worth a test of its own because nothing else exercises it: the steps that
    write these have no credentials and the steps that read them do nothing
    else, so a mismatch here is a publication that patches against a directory
    with no postings pages in it and quietly rebuilds every time.
    """
    import subprocess
    import sys

    served.database.commit("a starting point")
    served.publish()
    parent = tmp_path / "parent.json"
    parent.write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(served.delta))
    )
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1)
    served.database.commit("add one")
    prior = tmp_path / "prior"
    prior.mkdir()
    tool = pathlib.Path(__file__).resolve().parents[1]

    named = []
    for pass_number in (1, 2):
        plan_file = tmp_path / f"plan{pass_number}.txt"
        subprocess.run(
            [sys.executable, "tools/stage_public.py", "--root", str(served.database.path),
             "--previous", str(parent), "--plan", str(plan_file),
             # The first pass has nothing to be given, which is what the
             # workflow does: it is the pass that names the heads.
             *(["--prior", str(prior)] if pass_number == 2 else [])],
            check=True, cwd=tool,
        )
        wanted = plan_file.read_text().splitlines()
        assert wanted == sorted(wanted)
        assert not any(line.startswith("/") or ".." in line.split("/") for line in wanted)
        _fetch(served.path, prior, wanted)
        named.append(wanted)

    assert head_path("palomar") in named[0]
    assert page_path("palomar", 0) in named[1]
    assert head_path("palomar") not in named[1], "it asked again for what it was given"


@pytest.mark.parametrize(
    ("relative", "field", "malformed"),
    [
        (head_path("palomar"), "results", "missing"),
        (head_path("palomar"), None, "null"),
        (page_path("palomar", 0), "postings", "wrong type"),
    ],
)
def test_a_malformed_prior_search_document_forces_a_full_rebuild(
    served, tmp_path, capsys, relative, field, malformed
):
    """Malformed heads and pages take the same safe path in the real two-pass flow.

    A head is read while planning the second fetch and again while staging; a
    page is read while staging. Neither may leak a structural exception or be
    interpreted as an empty postings sequence.
    """
    import subprocess
    import sys

    served.database.commit("a starting point")
    served.publish()
    target = served.path / relative
    if malformed == "null":
        target.write_text("null\n")
    else:
        document = json.loads(target.read_text())
        if malformed == "missing":
            del document[field]
        else:
            document[field] = {}
        target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1)
    served.database.commit("one more")

    parent = tmp_path / "parent.json"
    parent.write_bytes(
        release_delta.canonical_base_bytes(release_delta.base_of(served.delta))
    )
    prior = tmp_path / "prior"
    prior.mkdir()
    tool = pathlib.Path(__file__).resolve().parents[1]
    for pass_number in (1, 2):
        plan_file = tmp_path / f"malformed-plan{pass_number}.txt"
        completed = subprocess.run(
            [
                sys.executable,
                "tools/stage_public.py",
                "--root",
                str(served.database.path),
                "--previous",
                str(parent),
                "--plan",
                str(plan_file),
                *(["--prior", str(prior)] if pass_number == 2 else []),
            ],
            check=True,
            cwd=tool,
            text=True,
            capture_output=True,
        )
        _fetch(served.path, prior, plan_file.read_text().splitlines())
        if relative == head_path("palomar") and pass_number == 2:
            assert "a full rebuild" in completed.stdout
            assert not plan_file.read_text()

    site = tmp_path / "malformed-release"
    stage_public.stage_public(
        served.database.path,
        site,
        previous=release_delta.base_of(served.delta),
        prior=prior,
    )
    delta = release_delta.parse((site / "release-delta.json").read_bytes())

    assert "staging everything" in capsys.readouterr().out
    assert delta["parent"] is None
    assert _answers(site) == _answers(served.rebuild())


def test_the_plan_names_every_postings_page_the_release_reads(served):
    """Nothing else is fetched, so a plan that under-named a page would leave
    the stager treating a page that exists as one that never did, and writing
    it back with every posting already on it gone."""
    for serial in range(2, 8):
        served.database.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1, title="A hypergraph regularity lemma")
    served.database.commit("one more")
    served.publish(by_plan=True)

    assert served.delta["parent"] is not None, "it fell back to a rebuild"
    # The word every earlier result carries keeps every one of them: the page
    # was read, appended to and written back, not written afresh.
    assert _postings(served.path, "palomar") == [
        f"PALOMAR-2026-07-29-{serial:06d}-v1" for serial in range(1, 8)
    ]
    assert _postings(served.path, "hypergraph") == [f"{newcomer}-v1"]
    assert _postings(served.path, "regularity") == [f"{newcomer}-v1"]


def test_a_postings_page_that_has_gone_missing_rebuilds_rather_than_writing_it_back(
    served, capsys
):
    """The head this release was handed says the page is part of the sequence,
    so a fresh one written back in its place would publish a page carrying only
    what this release appended, and take every result already on it out of the
    only surface that finds them."""
    served.database.commit("a starting point")
    served.publish()
    (served.path / page_path("palomar", 0)).unlink()
    served.database.add_entry("PALOMAR-2026-07-29-000002", 1)
    served.database.commit("add one")

    served.publish()

    assert "staging everything" in capsys.readouterr().out
    assert _postings(served.path, "palomar") == [
        "PALOMAR-2026-07-29-000001-v1",
        "PALOMAR-2026-07-29-000002-v1",
    ]


def test_a_head_a_withdrawn_result_carries_and_that_is_gone_rebuilds(served, capsys):
    """For a word a published record carries, "never written" and "gone" are
    distinguishable: it was written when that record was published. Reading a
    missing one as a word nothing carries would find nothing to remove and
    leave the postings behind, which is the correlation a takedown exists to
    break."""
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1, title="A retracted counterexample")
    served.database.commit("register")
    served.publish()
    (served.path / head_path("counterexample")).unlink()
    _take_down(served.database, newcomer, 1)
    served.database.commit("withdraw it")

    served.publish()

    assert "staging everything" in capsys.readouterr().out
    assert not (served.path / head_path("counterexample")).exists()


def test_a_word_nobody_has_used_before_is_not_a_missing_document(served):
    """A word's head exists as soon as any served result carries the word, so
    its absence means none does. Treating that as drift would make every
    publication that introduces a word a full rebuild, and every publication
    introduces words."""
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1, title="A hypergraph regularity lemma")
    served.database.commit("a word the registry has never carried")

    served.publish(by_plan=True)

    assert served.delta["parent"] is not None, "it fell back to a rebuild"
    assert _postings(served.path, "hypergraph") == [f"{newcomer}-v1"]


# What patching writes, against what building the same dataset writes.


def test_a_chain_of_releases_writes_the_index_a_rebuild_writes(served, monkeypatch):
    """The one failure this design can have that nothing else would catch.

    A patched page and a rebuilt one are both well formed, so a disagreement
    between them shows up months later as a page nobody can explain. Pages of
    four rather than a hundred and twenty-eight, so that the sequence seals
    pages and the comparison is about more than one document.

    The takedown at the end is the case this could not answer while a
    withdrawal emptied its slot rather than closing the sequence up: the pages
    after the removal all shift by one, the last page of a word may go, and a
    word carried by nothing else goes entirely.

    The results are registered in increasing order here, which is what makes
    the two comparable at all: a page is filled in the order results were
    registered and a rebuild fills it in identifier order, so this measures
    whether patching *introduces* a divergence, against a history where there
    was none to begin with. `test_a_patched_index_finds_what_a_rebuild_finds`
    is the histories where there is.
    """
    monkeypatch.setattr(build_search, "PAGE_SIZE", 4)
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    for serial in range(2, 12):
        served.database.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
        served.database.commit(f"add {serial}")
        served.publish(by_plan=True)
    _take_down(served.database, "PALOMAR-2026-07-29-000006", 1)
    served.database.commit("withdraw one from the middle")
    served.publish(by_plan=True)

    assert served.delta["parent"] is None, "takedowns must take the full path"
    rebuilt = served.rebuild()
    assert _staged(served.path) == _staged(rebuilt), "the two publish different documents"
    for relative in _staged(served.path) + [STOPWORDS_PATH]:
        assert (served.path / relative).read_bytes() == (rebuilt / relative).read_bytes(), (
            f"{relative} differs from what a rebuild writes"
        )


def test_a_patched_index_finds_what_a_rebuild_finds(served, monkeypatch):
    """Byte for byte is more than this one surface can promise, and finding the
    same results is not.

    Results are registered in identifier order, but a version that supersedes
    an old result is appended at the end of the live sequence while a rebuild
    sorts it beside its earlier versions. So the counts a head carries are a
    fact about the sequence's history rather than about the active set, and
    what has to agree is what a reader can observe: which results each word
    finds.
    """
    monkeypatch.setattr(build_search, "PAGE_SIZE", 4)
    for serial in range(2, 9):
        served.database.add_entry(f"PALOMAR-2026-07-29-{serial:06d}", 1)
    served.database.commit("a starting point")
    served.publish(by_plan=True)
    served.database.add_entry("PALOMAR-2026-07-29-000004", 2)
    served.database.commit("a second version of one of them")
    served.publish(by_plan=True)
    _take_down(served.database, "PALOMAR-2026-07-29-000006", 1)
    served.database.commit("withdraw one")
    served.publish(by_plan=True)

    for relative in _staged(served.path):
        if relative.rsplit("/", 1)[-1] != "head.json":
            postings = _document(served.path, relative)["postings"]
            assert postings == sorted(postings), f"{relative} is not sorted"
    served.database.write_json("takedowns.json", {"schema_version": 1, "takedowns": []})
    served.database.commit("put it back")
    served.publish(by_plan=True)

    assert served.delta["parent"] is None, "restorations must take the full path"
    assert _answers(served.path) == _answers(served.rebuild())


def test_a_withdrawn_result_is_not_findable_by_any_of_its_words(served):
    """A takedown has to reach the index, and reaching it costs the record's
    own vocabulary rather than the registry's.

    Leaving the postings behind would be worse than it looks. A reader who
    fetched several words and intersected them would learn which words the
    withdrawn record carried, which is exactly the correlation a takedown
    exists to break.
    """
    newcomer = served.database.next_identifier()
    served.database.add_entry(
        newcomer, 1, title="A retracted counterexample", abstract="Withdrawn afterwards."
    )
    served.database.commit("register")
    served.publish(by_plan=True)
    assert _postings(served.path, "counterexample") == [f"{newcomer}-v1"]

    _take_down(served.database, newcomer, 1)
    served.database.commit("withdraw it")
    served.publish(by_plan=True)

    assert served.delta["parent"] is None, "takedowns must take the full path"
    # Nothing is left, head included. A head saying it has no results would
    # publish the fact that some withdrawn record carried that word, and the
    # head is now one number rather than two, so there is no gap under a word
    # to say how many withdrawn results carried it.
    for word in ("counterexample", "retracted", "withdrawn", "afterwards"):
        assert not (served.path / head_path(word)).exists(), word
        assert not (served.path / page_path(word, 0)).exists(), word
    assert served.delta["retired"] == []


def test_a_restored_result_is_findable_again(served):
    """A takedown never touches a file, so neither its arrival nor its reversal
    is visible in a diff; both come from the difference between the parent
    release's withdrawn set and this one's."""
    newcomer = served.database.next_identifier()
    served.database.add_entry(newcomer, 1, title="A retracted counterexample")
    _take_down(served.database, newcomer, 1)
    served.database.commit("register and withdraw")
    served.publish(by_plan=True)
    assert not (served.path / head_path("counterexample")).exists()

    served.database.write_json("takedowns.json", {"schema_version": 1, "takedowns": []})
    served.database.commit("put it back")
    served.publish(by_plan=True)

    assert served.delta["parent"] is None, "restorations must take the full path"
    assert _postings(served.path, "counterexample") == [f"{newcomer}-v1"]
