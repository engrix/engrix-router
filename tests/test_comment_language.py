"""Guard: `#` comments are Indonesian (ADR-0000), and the rule has teeth.

The docstring half of the language policy is checked by test_language_policy.py through
the AST. This file covers the half the AST cannot see: comments. Without a check here the
rule was pure intention, and it drifted -- files written recently carried English
comments while the ported ones carried Indonesian, so the same repo read two ways.

HOW A COMMENT IS JUDGED (this is the part that has to be right, or the guard becomes noise):

* a comment block = consecutive `#` lines; wrapped continuation lines are judged with
  their block, never alone;
* a block is PROSE when it holds at least 3 alphabetic words of 3+ letters -- bare
  citations (`# executors/qoder.js:613`), numbers, paths and tool directives are not prose;
* prose counts as English when it carries >=2 distinct English function words AND no
  Indonesian word from the policy vocabulary. Detecting English is deliberate: an
  Indonesian comment legitimately contains tech words (stream, chunk, provider, payload),
  and requiring "a local-language word on every line" would flag correct comments.

`ALLOWED_ENGLISH` is an exact-match, shrink-only registry, same pattern as DEFERRED_*:
leaving a comment English means adding its exact text here, and the list may only shrink.
"""
from __future__ import annotations

import io
import pathlib
import re
import tokenize

from tests.test_language_policy import INDONESIAN

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIPPED = (ROOT / "src" / "engrix_router", ROOT / "scripts")

# Kata fungsi/bantu English. Sengaja bukan daftar "semua kata English": istilah teknis
# (stream, provider, timeout) boleh muncul di komentar Indonesia, kata "with/this/must"
# tidak.
EN_FUNC = re.compile(
    r"\b(the|and|are|was|were|been|being|have|has|had|does|doing|did|this|that|these|"
    r"those|with|from|into|onto|which|whose|would|could|should|must|not|nor|but|when|"
    r"while|then|than|them|they|their|we|our|you|your|his|her|its|because|before|after|"
    r"over|under|never|always|some|each|every|other|another|same|such|only|just|very|"
    r"both|either|about|above|below|between|using|used|uses|where|what|why|how)\b",
    re.I,
)

# Komentar yang emang boleh English. Eksak, dan cuma boleh mengecil.
ALLOWED_ENGLISH: set[str] = set()

WORDS = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")


def _comment_blocks(path: pathlib.Path) -> list[tuple[int, str]]:
    """
    Consecutive comment lines as one block.

    Per-line judging is wrong: a wrapped Indonesian comment has continuation lines
    with no distinctive word in them ("units. Names are matched generically..."), and
    treating each line alone would demand Indonesian in every single line. A block
    counts as Indonesian if ANY of its lines says so.
    """
    text = path.read_text(encoding="utf-8")
    try:
        tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                  if t.type == tokenize.COMMENT]
    except (SyntaxError, ValueError):
        return []
    blocks: list[tuple[int, str]] = []
    last_line = 0
    for token in tokens:
        body = token.string.lstrip("#").strip()
        line = token.start[0]
        if not body or "noqa" in body or body.startswith(("type:", "pragma:", "ruff:", "pylint:")):
            last_line = line
            continue
        if blocks and line == last_line + 1:
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + " " + body)
        else:
            blocks.append((line, body))
        last_line = line
    return blocks


def _is_prose(body: str) -> bool:
    """Prose = at least 3 real words; a bare citation, number or path is not prose."""
    return len(WORDS.findall(body)) >= 3


def _looks_english(body: str) -> bool:
    """Two or more distinct English function words, and no Indonesian vocabulary."""
    if INDONESIAN.search(body):
        return False
    return len({m.group(0).lower() for m in EN_FUNC.finditer(body)}) >= 2


def test_comments_in_shipped_code_are_indonesian():
    offenders = []
    for base in SHIPPED:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            rel = path.relative_to(ROOT).as_posix()
            for line, body in _comment_blocks(path):
                if not _is_prose(body) or not _looks_english(body):
                    continue
                if body in ALLOWED_ENGLISH:
                    continue
                offenders.append(f"{rel}:{line}: {body[:72]!r}")
    assert not offenders, (
        "komentar `#` harus Indonesia (ADR-0000) -- docstring/string yang ke-publish "
        "tetap English, itu dijaga test sebelah:\n  " + "\n  ".join(offenders)
    )


def test_english_comment_allowlist_is_still_needed():
    """Entri yang udah dibenerin harus dihapus dari ALLOWED_ENGLISH; registry gak boleh jadi gudang."""
    present = {body for base in SHIPPED for path in sorted(base.rglob("*.py"))
               if "__pycache__" not in str(path) for _line, body in _comment_blocks(path)}
    dead = sorted(ALLOWED_ENGLISH - present)
    assert not dead, (
        "entri ALLOWED_ENGLISH yang gak ada lagi di tree, hapus:\n  " + "\n  ".join(dead)
    )
