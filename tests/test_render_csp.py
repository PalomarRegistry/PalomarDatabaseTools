"""One render policy, written in two languages, kept identical by this file.

`tools/render_validation.py` decides which policy a published render bundle is
allowed to carry, and refuses the bundle otherwise. The Worker sends the same
policy as a response header, because the bundle's `<meta>` binds only from the
moment the parser reaches it while a header binds before the first byte is
parsed. A Worker cannot import a Python constant, so the string is written twice; a
weaker policy in one of them than in the other would mean a render was
validated against one thing and served under another, and nothing else would
notice.
"""

from __future__ import annotations

import pathlib
import re

from render_validation import RENDER_CSP

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker" / "src" / "index.ts"


def worker_render_csp() -> str:
    """The `RENDER_CSP` array in the Worker, joined as the Worker joins it."""
    source = WORKER.read_text(encoding="utf-8")
    body = re.search(r"const RENDER_CSP = \[(.*?)\]\.join\(\"; \"\);", source, re.DOTALL)
    assert body is not None, "the Worker no longer builds RENDER_CSP from a list"
    return "; ".join(re.findall(r'"([^"]+)"', body.group(1)))


def test_the_worker_sends_the_policy_the_validator_requires():
    assert worker_render_csp() == RENDER_CSP


def test_the_worker_says_who_may_frame_a_render():
    """The one directive the bundle's own policy cannot carry.

    `frame-ancestors` is ignored in a `<meta>` policy, so until it was sent as
    a header nothing said who may embed a submitter's rendered Lean. One origin,
    because there is one site, and it is a different origin from the data by
    design. Read out of the declaration rather than looked for anywhere in the
    file, because the file talks about this at length and a comment is not a
    policy.
    """
    source = WORKER.read_text(encoding="utf-8")
    declared = re.search(r'const RENDER_FRAME_ANCESTORS = "([^"]+)";', source)
    assert declared is not None, "the Worker no longer declares who may frame a render"
    assert declared.group(1) == "frame-ancestors https://palomar-registry.org"
