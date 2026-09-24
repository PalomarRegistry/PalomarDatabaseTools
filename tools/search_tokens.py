"""Shared publisher tokenization; query parity is checked against Worker fixtures."""
import re
import unicodedata

SEPARATOR_RE = re.compile(r"[^a-z0-9]+")

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

def _fold(text: str) -> str:
    """Lowercase, with accents folded away rather than split on.

    A registry of mathematics is full of Erdős, Kähler and Poincaré. Splitting
    on a combining mark turns the first into `erd` and `s`, neither of which
    anyone types; dropping the mark turns it into `erdos`, which everyone does.

    The Worker performs the same three steps in the same order, and the two
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
    """ASCII alphanumeric words of 2–32 characters, without stemming."""
    return [word for word in SEPARATOR_RE.split(_fold(text)) if 2 <= len(word) <= 32]

