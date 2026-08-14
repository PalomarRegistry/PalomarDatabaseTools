#!/usr/bin/env python3
"""Full-text search as one append-only postings sequence per term.

A search index is the one derived surface that is naturally quadratic. The
obvious shape -- a document per term listing every result carrying it, or worse
a single inverted file -- is rewritten in full every time anything is
published, so the registry pays O(S) per accepted result and O(S^2) over its
life, which is the cost this repository has already removed from the release
manifest, the feeds, the version indexes and browsing.

So a term's postings are a *sequence of pages*, sealed at `PAGE_SIZE` and
never touched again, plus a head of five fields naming how many there are.
Publishing a result appends to the open page of each distinct term in it and
rewrites that term's head. That is two objects per term of one record: about
three hundred objects and six hundred kilobytes for a full-length abstract,
and that count is a property of the record, not of the registry. A hundred
thousand results later it is still two objects per term.

**There is no term dictionary, and adding one would undo all of this.** A
document naming every known term grows with the vocabulary, the vocabulary
grows without bound with the registry, and rewriting it on every publication
is the exact super-linear term this design exists to avoid. The client
therefore hashes nothing and looks nothing up: it asks for
`search/t/<term>/head.json` directly and reads a 404 as "no result carries
this word". Every part of the path grammar exists to make that request safe to
construct without a dictionary to check it against.

The stopword list is published, at `search/stopwords.json`. That is not a term
dictionary and cannot grow into one: it is a fixed editorial choice of a few
hundred function words, constant in size. It is staged and rewritten on every
publication, adding one constant-sized stable-object write; a term dictionary
would also be rewritten on every publication but would grow with the registry's
vocabulary. Publishing the fixed list closes a hole a reader could not diagnose.
A dropped word has no head, and a missing head is also what a word nothing
carries looks like, so a client that inferred "stopword" from a 404 answered
"the ring" against a record whose text happens not to contain "the" by
reporting that "the" is unfindable. One source of truth, fetched rather than
copied, because two copies across two independently deployed repositories
drift.

Postings carry the versioned identifier rather than a registration ordinal.
The identifier resolves straight to `entries/<id>-vN.json`, so a reader turns
a posting into a result without a second whole-registry surface mapping an
ordinal back to a record. What it costs is the ability to binary-search for a
posting, which is why a withdrawal below is a scan.

Nothing here stems. This is mathematical text, and the words that matter most
are the ones a stemmer damages worst: `ring` and `rings`, `field` and
`fields`, `group` and `grouping` are not the same query, and conflating them
makes the commonest terms in the registry commoner still. Folding accents is
done, because Erdős and Kähler are names people type without them.

Nothing under `search/` is kept in the database. It was, because a page is
appended to and a publication runs in a fresh checkout with nothing else to
start from. That committed about fifty kilobytes of new blobs per accepted
result into a history which keeps every version of every changed page: five
gigabytes of history at a hundred thousand results, against GitHub's own five
gigabyte guidance, and unrecoverable once merged. The pages a release appends
to come from the release being served instead, fetched by name by the same
four steps every other surface here uses.
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from typing import Any, Iterable

from patch_surfaces import Rebuild

SCHEMA_VERSION = 1
TERM_ROOT = "search/t"
# The words the indexer drops, published so that a reader can drop them from a
# query instead of inferring them from a missing head.
STOPWORDS_PATH = "search/stopwords.json"

# What a publication pays per term, in one number. The open page is rewritten
# whenever the term appears in a new result, so the page size *is* the cost of
# a term: a hundred and twenty-eight postings is about four and a half
# kilobytes, and the six hundred kilobytes that a full-length abstract's worth
# of terms costs is the same six hundred kilobytes at a hundred thousand
# results. Larger pages are fewer requests for a reader and more bytes for
# every publication; this is the balance point, not a law.
PAGE_SIZE = 128
# Two hundred and sixty thousand results under one word. Long before that the
# shape has to change, and a publication that stops is how anyone finds out;
# the alternative is a term whose sequence quietly stops growing, which takes
# results out of the only surface that claims to find them.
MAX_PAGES = 2048
# A term is short, lowercase and alphanumeric, and anything else is dropped
# rather than escaped. That is what lets the Worker constrain the path with a
# plain regular expression and lets a browser build one from typed text
# without an encoding step that either side could get wrong.
TERM_RE = re.compile(r"[a-z0-9]{2,32}")
SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
# The schema bounds an abstract at ten thousand characters and a title at three
# hundred, so a record cannot carry more than a few thousand distinct terms.
# The bound is restated here because it is this file that turns each of them
# into two objects, and a schema that relaxed would otherwise quietly turn one
# submission into an unbounded publication.
MAX_TERMS_PER_RECORD = 4000

# Function words only. A stopword list is a claim that a word carries no
# information *in this corpus*, and in a registry of formalized mathematics
# almost every content word does: `set`, `field`, `group`, `ring`, `order` and
# `normal` are all ordinary English and all load-bearing. So this list stops at
# the words English needs for grammar, and nothing is added to it to make the
# index smaller.
STOPWORDS = frozenset(
    """
    about above after again against all am an and any are as at be because been
    before being below between both but by can cannot did do does doing down
    during each few for from further had has have having he her here hers him
    his how if in into is it its itself just me more most my no nor not of off
    on once only or other our ours out over own same she should so some such
    than that the their theirs them then there these they this those through to
    too under until up very was we were what when where which while who whom
    why will with you your yours
    """.split()
)

# One record, as the stager holds it: its index summary and the record itself.
Record = tuple[dict[str, Any], dict[str, Any]]


def _fold(text: str) -> str:
    """Lowercase, with accents folded away rather than split on.

    A registry of mathematics is full of Erdős, Kähler and Poincaré. Splitting
    on a combining mark turns the first into `erd` and `s`, neither of which
    anyone types; dropping the mark turns it into `erdos`, which everyone does.

    The browser performs the same three steps in the same order, and the two
    must agree exactly: a query folded differently is a request for a term the
    indexer never wrote, which is indistinguishable from no results at all.
    `str.lower` rather than `str.casefold`, because casefold maps ß to ss and
    JavaScript's `toLowerCase` does not.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    ).lower()


def tokens(text: str) -> list[str]:
    """Every word of a text that this index is able to name.

    Anything outside `[a-z0-9]{2,32}` is dropped rather than encoded. A
    percent-escaped or base32'd term would let a record put arbitrary bytes in
    a served path, and would make the client's job constructing that path a
    piece of shared encoding logic instead of a regular expression.
    """
    return [word for word in SEPARATOR_RE.split(_fold(text)) if 2 <= len(word) <= 32]


def terms_of(entry: dict[str, Any]) -> set[str]:
    """Which words a record is findable by.

    Title, abstract and author names. The first two are what the result *is*;
    the third is the question readers actually arrive with, an author's name
    is already the most public part of a record, and a name is exactly the kind
    of rare token this shape answers cheaply.

    Classification codes are deliberately not indexed. `math.NT` and `11N13`
    already have surfaces that answer them exactly -- `subjects/` and the
    category feeds -- and those answer "everything under this code", which a
    word search cannot. Tokenizing them would instead file every result in the
    registry under `math`, which is a term that costs a page on every
    publication and distinguishes nothing.

    Theorem and definition names are not indexed either. They are Lean
    identifiers, and splitting `Erdos.unit_distance` into `erdos`, `unit` and
    `distance` does not give a reader who wants that declaration a way to ask
    for it; it gives them three common English words. An exact identifier
    lookup is a different surface, and a bad approximation of it here would be
    worse than none.
    """
    texts = [str(entry.get("title") or ""), str(entry.get("abstract") or "")]
    authors = entry.get("authors")
    if isinstance(authors, list):
        texts.extend(
            str(author.get("name") or "") for author in authors if isinstance(author, dict)
        )
    found = {
        word
        for text in texts
        for word in tokens(text)
        if word not in STOPWORDS
    }
    if len(found) > MAX_TERMS_PER_RECORD:
        raise ValueError(
            f"{entry.get('id')}-v{entry.get('version')} carries {len(found)} distinct terms, "
            f"more than the {MAX_TERMS_PER_RECORD} one record may contribute; the schema is "
            "supposed to make that impossible, so this is a schema change nobody costed"
        )
    return found


def posting_of(summary: dict[str, Any]) -> str:
    return f"{summary['id']}-v{int(summary['version'])}"


def head_path(term: str) -> str:
    return f"{TERM_ROOT}/{term}/head.json"


def page_path(term: str, number: int) -> str:
    return f"{TERM_ROOT}/{term}/{number}.json"


def _by_term(records: Iterable[Record]) -> dict[str, list[str]]:
    """Which postings each word of a set of records gains or loses."""
    found: dict[str, list[str]] = {}
    for summary, entry in records:
        for term in terms_of(entry):
            found.setdefault(term, []).append(posting_of(summary))
    return {term: sorted(postings) for term, postings in found.items()}


def _invalid(relative: str, reason: str) -> Rebuild:
    return Rebuild(f"the release being served has an invalid {relative}: {reason}")


def _read(prior: pathlib.Path | None, relative: str) -> dict[str, Any] | None:
    if prior is None:
        return None
    path = prior / relative
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as reason:
        # Patching against half a page would write it back as the whole page,
        # taking every posting already on it out of the only surface that
        # finds them. A rebuild states the sequence outright.
        raise Rebuild(f"{relative} is not a document this release can read: {reason}")
    if not isinstance(document, dict):
        raise _invalid(relative, "document is not an object")
    return document


def _integer(value: object, relative: str, field: str) -> int:
    if type(value) is not int or value < 0:
        raise _invalid(relative, f"{field} is not a non-negative integer")
    return value


def _validate_head(document: object, term: str, relative: str) -> dict[str, Any]:
    keys = {"schema_version", "term", "page_size", "pages", "results"}
    if not isinstance(document, dict) or set(document) != keys:
        raise _invalid(relative, "document has the wrong fields")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise _invalid(relative, "schema_version is not the current version")
    if not isinstance(document["term"], str) or document["term"] != term:
        raise _invalid(relative, "term does not match its path")
    if type(document["page_size"]) is not int or document["page_size"] != PAGE_SIZE:
        raise _invalid(relative, "page_size is not the current size")
    pages = _integer(document["pages"], relative, "pages")
    results = _integer(document["results"], relative, "results")
    expected_pages = (results + PAGE_SIZE - 1) // PAGE_SIZE
    if pages != expected_pages or pages > MAX_PAGES:
        raise _invalid(relative, "pages and results do not describe one sequence")
    return dict(document)


def _validate_page(
    document: object, term: str, number: int, relative: str
) -> dict[str, Any]:
    keys = {"schema_version", "term", "page", "postings"}
    if not isinstance(document, dict) or set(document) != keys:
        raise _invalid(relative, "document has the wrong fields")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise _invalid(relative, "schema_version is not the current version")
    if (
        not isinstance(document["term"], str)
        or document["term"] != term
        or type(document["page"]) is not int
        or document["page"] != number
    ):
        raise _invalid(relative, "term or page does not match its path")
    rows = document["postings"]
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, str) for row in rows)
        or rows != sorted(set(rows))
        or len(rows) > PAGE_SIZE
    ):
        raise _invalid(relative, "postings is not a bounded sorted set of strings")
    return dict(document)


def _fresh_head(term: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "term": term,
        "page_size": PAGE_SIZE,
        "pages": 0,
        # How many results carry the word, which is also how many postings the
        # sequence holds and therefore what fixes the number of pages. There
        # were two numbers here while a withdrawal emptied its slot rather than
        # closing the sequence up, and their difference was how many withdrawn
        # results carried the word. One number cannot say that.
        "results": 0,
    }


def head_paths(records: Iterable[Record]) -> list[str]:
    """The heads a release has to read, named from the changed records alone.

    This is what the plan can say with no bucket credentials: the words of a
    record are in the record, and a word's head is at a path built from the
    word. Which *page* it then touches is not knowable here. The open page's
    number comes from how many postings the word already has, which is a fact
    about the registry rather than about the record, and the only document
    that states it is the head. So the plan is worked out twice, and
    `page_paths` is the second time.
    """
    return sorted({head_path(term) for term in _by_term(records)})


def page_paths(
    prior: pathlib.Path,
    additions: Iterable[Record],
    withdrawals: Iterable[Record],
) -> list[str]:
    """The postings pages a release has to read, given the heads it now holds.

    An append reads the open page and no other, so its number comes straight
    out of how many postings the word already has. A withdrawal is a scan, because the pages are in
    registration order and a posting is an identifier, so there is nothing to
    binary-search on: it names the whole sequence of each word the withdrawn
    record carries. That is the record's own vocabulary times the length of
    those words' sequences, once per takedown and never on a publication.
    """
    wanted: set[str] = set()
    for term, postings in _by_term(additions).items():
        relative = head_path(term)
        head = _read(prior, relative)
        # A word with no head here has no postings and so has no page to fetch,
        # and this is the only place that could get that wrong: guessing a
        # number instead would name page zero of a word whose head has simply
        # not been fetched yet, and hand the stager somebody else's page to
        # append to. A page is named because a head said so, or not at all.
        if head is None:
            continue
        head = _validate_head(head, term, relative)
        held = int(head["results"])
        first = held // PAGE_SIZE
        last = (held + len(postings) - 1) // PAGE_SIZE
        wanted.update(page_path(term, number) for number in range(first, last + 1))
    for term in _by_term(withdrawals):
        relative = head_path(term)
        head = _read(prior, relative)
        if head is None:
            continue
        head = _validate_head(head, term, relative)
        wanted.update(page_path(term, number) for number in range(int(head["pages"])))
    return sorted(wanted)


class _Postings:
    """The pages this release touches, read from the release being served.

    Reads fall through to the documents the fetch step put in `prior`; writes
    are collected and flushed at the end, so a run costs the memory of what it
    touched. For a publication that is one open page per word of one record.
    For a rebuild there is no `prior` at all and every page is written from
    nothing, which is the O(S) a rebuild is.
    """

    def __init__(self, prior: pathlib.Path | None, output: pathlib.Path) -> None:
        self.prior = prior
        self.output = output
        # A value of None means the object is going. Held in the same dictionary
        # as the writes so that a word withdrawn and then appended to in one
        # release cannot be read back as the document the withdrawal removed.
        self.dirty: dict[str, Any | None] = {}

    def read(self, relative: str) -> Any | None:
        if relative in self.dirty:
            return self.dirty[relative]
        return _read(self.prior, relative)

    def write(self, relative: str, document: Any) -> None:
        self.dirty[relative] = document

    def retire(self, relative: str) -> None:
        self.dirty[relative] = None

    def flush(self) -> tuple[list[str], list[str]]:
        written, retired = [], []
        for relative, document in sorted(self.dirty.items()):
            if document is None:
                retired.append(relative)
                continue
            path = self.output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            written.append(relative)
        return written, retired


def _head(postings: _Postings, term: str) -> dict[str, Any]:
    """The head of one word, or a fresh one meaning nothing carries it yet.

    A head that is not there is read as one that was never written, which is
    the reading a code's front page gets for the same reason: a word's head
    exists as soon as any served result carries it, so its absence means none
    does. Calling that drift instead would make every publication that
    introduces a word a full rebuild, and every publication introduces words.
    """
    relative = head_path(term)
    document = postings.read(relative)
    return (
        _fresh_head(term)
        if document is None
        else _validate_head(document, term, relative)
    )


def _page(
    postings: _Postings, term: str, number: int, head: dict[str, Any]
) -> dict[str, Any]:
    document = postings.read(page_path(term, number))
    if document is None:
        if number < int(head["pages"]):
            # The head this release was handed says this page is part of the
            # sequence. Writing a fresh one back would publish a page carrying
            # only what this release appended, and take every posting already
            # on it out of the only surface that finds them.
            raise Rebuild(
                f"{page_path(term, number)} is inside a published sequence and is not there"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "term": term,
            "page": number,
            "postings": [],
        }
    return _validate_page(document, term, number, page_path(term, number))


def _append(postings: _Postings, term: str, posting: str) -> None:
    """Add one result to one term, touching one page and one head."""
    head = _head(postings, term)
    number = int(head["results"]) // PAGE_SIZE
    if number >= MAX_PAGES:
        raise ValueError(
            f"the postings for {term!r} would need page {number}, past the {MAX_PAGES} "
            "one term may have; the index needs a different shape before this word does"
        )
    page = _page(postings, term, number, head)
    rows = page["postings"]
    if posting in rows:
        # Nothing here is allowed to be nearly idempotent. A duplicate posting
        # shows a reader the same result twice and, worse, means an append ran
        # against a base that already had it -- so the run's idea of what it
        # was adding is wrong, and stopping is the only honest answer.
        raise ValueError(f"{posting} is already on page {number} of {term!r}")
    # Sorted within the page, which costs nothing and buys the reader a
    # checkable property: an identifier begins with its registration date, so a
    # page in identifier order is a page in date order, and a validator can
    # reject a page that repeats a posting or pads itself out of order. Across
    # pages the order is registration order, which is what makes them sealed.
    rows.append(posting)
    rows.sort()
    head["results"] = int(head["results"]) + 1
    head["pages"] = (int(head["results"]) + PAGE_SIZE - 1) // PAGE_SIZE
    postings.write(page_path(term, number), page)
    postings.write(head_path(term), head)


def _remove(postings: _Postings, term: str, posting: str) -> bool:
    """Take one result out of one term, and close the sequence up behind it.

    A scan, because the pages are in registration order and a posting is an
    identifier: there is no key to binary-search on. It reads the pages of the
    withdrawn record's own terms, so the cost is the sum of those terms'
    lengths -- a few megabytes at a hundred thousand results, once per takedown
    and never on a publication. That is the price of registration order, and
    registration order is what lets a page be sealed.

    The slot is closed up rather than emptied. Emptying it was cheaper by every
    page after the removal, and it left `appended` above `results`, which is a
    published number: the gap under each word says how many withdrawn results
    carried it, so a reader who read the gaps across the index recovered the
    withdrawn record's vocabulary. An abstract's vocabulary is distinctive, and
    the tombstone already publishes the identifier and the date, so that is a
    good deal of what a withdrawal is for. This costs writes on a path that was
    already reading every one of these pages. Re-sorting each rewritten page
    preserves its checkable local invariant, while the sequence across pages
    remains append-ordered history rather than the globally sorted bytes a
    rebuild happens to write; both find exactly the same active postings.
    """
    relative = head_path(term)
    head = postings.read(relative)
    if head is None:
        # Every word a published record carries was written when it was
        # published, so this head is missing rather than absent. Leaving the
        # postings behind is what a takedown exists to prevent.
        raise Rebuild(f"{head_path(term)} is carried by a withdrawn result and is not there")
    head = _validate_head(head, term, relative)
    was = int(head["pages"])
    pages = [_page(postings, term, number, head)["postings"] for number in range(was)]
    rows = [row for page in pages for row in page]
    if posting not in rows:
        return False
    rows.remove(posting)
    chunks = [
        sorted(rows[start : start + PAGE_SIZE])
        for start in range(0, len(rows), PAGE_SIZE)
    ]
    for number, chunk in enumerate(chunks):
        if chunk != pages[number]:
            postings.write(
                page_path(term, number),
                {
                    "schema_version": SCHEMA_VERSION,
                    "term": term,
                    "page": number,
                    "postings": chunk,
                },
            )
    for number in range(len(chunks), was):
        postings.retire(page_path(term, number))
    if rows:
        head["results"] = len(rows)
        head["pages"] = len(chunks)
        postings.write(head_path(term), head)
    else:
        # The last result carrying this word has gone, so the word goes with
        # it. A head left behind saying it has no results is the word's own
        # existence surviving a takedown, which is the thing above.
        postings.retire(head_path(term))
    return True


def stage_stopwords(output: pathlib.Path) -> str:
    """Publish the list itself, so that no client keeps a copy of it.

    It is staged on every release and the publisher rewrites every staged stable
    object. This is one constant-sized object write per publication, unlike the
    refused term dictionary whose bytes would grow with the vocabulary.
    """
    path = output / STOPWORDS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "stopwords": sorted(STOPWORDS)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return STOPWORDS_PATH


def build_search(output: pathlib.Path, records: Iterable[Record]) -> list[str]:
    """State the whole index, from every record being served.

    The O(S) path, and the one every uncertainty falls back to. Records are
    indexed in identifier order, so that a rebuild is a function of the active
    set and not of the order the records happened to be visited in.
    """
    postings = _Postings(None, output)
    for summary, entry in sorted(records, key=lambda pair: posting_of(pair[0])):
        for term in sorted(terms_of(entry)):
            _append(postings, term, posting_of(summary))
    written, _retired = postings.flush()
    return written + [stage_stopwords(output)]


def patch_search(
    output: pathlib.Path,
    prior: pathlib.Path,
    additions: Iterable[Record],
    withdrawals: Iterable[Record],
) -> list[str]:
    """Append and remove what this release changes.

    Returns the objects the release takes away, which the delta declares and
    the publisher deletes. The fixed stopword document is also staged and
    rewritten on every release so clients have one deployed source of truth.

    Every page read here was named by the plan and fetched by name, so a plan
    that under-named one would leave this treating a page that exists as one
    that never did. That is what the `Rebuild` in `_page` refuses to do.
    """
    postings = _Postings(prior, output)
    # Withdrawals first, so that a record which arrived and was taken down
    # between two publications cannot be appended by one loop and hunted for by
    # the other in the same run.
    for summary, entry in sorted(withdrawals, key=lambda pair: posting_of(pair[0])):
        for term in sorted(terms_of(entry)):
            _remove(postings, term, posting_of(summary))
    for summary, entry in sorted(additions, key=lambda pair: posting_of(pair[0])):
        for term in sorted(terms_of(entry)):
            _append(postings, term, posting_of(summary))
    _written, retired = postings.flush()
    stage_stopwords(output)
    return retired
