"""Parse a model's find-and-replace response and apply it to the document.

FIND is located by the first of these strategies that succeeds; more than one match is an error:
  1. exact       - FIND occurs verbatim exactly once
  2. normalized  - FIND occurs once after normalizing whitespace, quote styles and dashes
                   ("Claude's" = "Claude’s", " -- " = "—"); a position map recovers the exact span
  3. fuzzy       - for other small differences (a dropped word): one passage scores >= the
                   threshold (rapidfuzz partial_ratio) and no other, non-overlapping passage does.
                   Its span is re-aligned and accepted only if both edges and the length agree
                   with FIND; otherwise the model is asked to retry. A misplaced edit would corrupt
                   the document silently for every later round, so precision beats coverage here.
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

from .config import FUZZY_MATCH_THRESHOLD

NO_CHANGE = "[NO CHANGE]"
DELIMITERS = ["CHANGE DESCRIPTION", "FIND", "REPLACE"]
# A delimiter on its own line, tolerating Markdown decoration such as **---FIND---**.
_DELIMITER = re.compile(r"^[ \t*_#>`]*---(CHANGE DESCRIPTION|FIND|REPLACE)---[ \t*_`]*$", re.MULTILINE)
_FENCED = re.compile(r"```[\w-]*\n(.*)\n```", re.DOTALL)


class EditError(ValueError):
    """The response could not be turned into an edit. The message is shown to the model on retry."""


@dataclass(frozen=True)
class ProposedEdit:
    description: str
    find: str
    replace: str

    @property
    def is_no_change(self) -> bool:
        return self.find == NO_CHANGE and self.replace == NO_CHANGE


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    strategy: str  # "exact" | "normalized" | "fuzzy"
    score: float = 100.0


def parse_response(text: str) -> ProposedEdit:
    delimiters = list(_DELIMITER.finditer(text))
    if [d.group(1) for d in delimiters] != DELIMITERS:
        missing = [f"---{name}---" for name in DELIMITERS if name not in {d.group(1) for d in delimiters}]
        if missing:
            raise EditError(f"Response is missing the {', '.join(missing)} delimiter(s), each on its own line.")
        raise EditError("Use each of the three delimiters exactly once, in order, each on its own line.")
    bounds = [d.end() for d in delimiters]
    starts = [d.start() for d in delimiters[1:]] + [len(text)]
    description, find, replace = (_unfence(text[a:b].strip()) for a, b in zip(bounds, starts))
    if not find:
        raise EditError("The FIND section is empty; it must quote the passage to replace.")
    if (find == NO_CHANGE) != (replace == NO_CHANGE):
        raise EditError(f"{NO_CHANGE} must appear in both FIND and REPLACE, or in neither.")
    return ProposedEdit(description, find, replace)


def _unfence(section: str) -> str:
    """Drop a code fence wrapped around a whole section (the documents themselves contain none)."""
    fenced = _FENCED.fullmatch(section)
    return fenced.group(1).strip() if fenced and "```" not in fenced.group(1) else section


def locate(document: str, find: str) -> Match:
    find = find.strip()  # parse_response already strips; this keeps locate safe on its own
    exact = document.count(find)
    if exact == 1:
        start = document.index(find)
        return Match(start, start + len(find), "exact")
    if exact > 1:
        raise EditError(f"FIND text matched {exact} locations; include more context to make it unique.")

    normal_document, starts, ends = normalize(document)
    normal_find = normalize(find)[0]
    count = normal_document.count(normal_find)
    if count == 1:
        i = normal_document.index(normal_find)
        return Match(starts[i], ends[i + len(normal_find) - 1], "normalized")
    if count > 1:
        raise EditError(f"FIND text matched {count} locations; include more context to make it unique.")

    return _fuzzy(document, find)


# Runs of dashes absorb the spaces around them, so " -- ", "—" and " – " all become "-".
_TOKENS = re.compile(r"\s*[-\u2010-\u2015\u2212]+\s*|\s+|\u2026|.", re.DOTALL)
_QUOTES = str.maketrans("\u2018\u2019\u201a\u201b\u2032\u201c\u201d\u201e\u201f\u2033", "'''''\"\"\"\"\"")


def normalize(text: str) -> tuple[str, list[int], list[int]]:
    """Normalized text, plus for each of its characters the start and end of the original text it
    came from (with surrounding whitespace excluded, so spans never swallow paragraph breaks)."""
    out, starts, ends = [], [], []
    for token in _TOKENS.finditer(text):
        raw = token.group()
        if raw.isspace():
            normal = " "
        elif raw.strip()[0] in "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
            normal = "-"
        elif raw == "\u2026":
            normal = "..."
        else:
            normal = raw.translate(_QUOTES)
        core_start = token.start() + len(raw) - len(raw.lstrip())
        core_end = token.end() - (len(raw) - len(raw.rstrip())) if raw.strip() else token.end()
        for char in normal:
            out.append(char)
            starts.append(core_start)
            ends.append(core_end)
    return "".join(out), starts, ends


def _fuzzy(document: str, find: str) -> Match:
    best = fuzz.partial_ratio_alignment(find, document, score_cutoff=FUZZY_MATCH_THRESHOLD)
    if best is None:
        raise EditError(
            "FIND text was not found in the document, even ignoring whitespace, quote style and small differences. "
            "Copy the passage verbatim."
        )
    # Blank out the best passage and look again: a second strong match means the FIND is ambiguous.
    masked = document[: best.dest_start] + "\0" * (best.dest_end - best.dest_start) + document[best.dest_end :]
    if fuzz.partial_ratio_alignment(find, masked, score_cutoff=FUZZY_MATCH_THRESHOLD):
        raise EditError("FIND text approximately matches more than one passage; include more context.")

    start, end = _realign(document, find, best.dest_start, best.dest_end)
    passage = document[start:end]
    score = fuzz.ratio(find, passage)
    edge = max(3, min(12, len(find) // 4))
    normal_find, normal_passage = normalize(find)[0], normalize(passage)[0]
    if (
        score < FUZZY_MATCH_THRESHOLD
        or abs(len(passage) - len(find)) > max(8, len(find) // 50)
        or normal_find[:edge] != normal_passage[:edge]
        or normal_find[-edge:] != normal_passage[-edge:]
    ):
        raise EditError(
            "FIND text is close to a passage in the document but not close enough to place the edit precisely. "
            "Copy the passage verbatim, character for character, especially its first and last words."
        )
    return Match(start, end, "fuzzy", round(score, 2))


def _realign(document: str, find: str, start: int, end: int) -> tuple[int, int]:
    """partial_ratio aligns a window exactly as long as FIND, so when the true passage is longer or
    shorter (a dropped word) its ends are off. Align FIND against a widened window instead, take
    the span from its first to its last substantial matching run, then extend each end while the
    document keeps agreeing with FIND character for character."""
    slack = len(find) // 10 + 20
    offset = max(0, start - slack)
    window = document[offset : min(len(document), end + slack)]
    shortest = max(3, min(12, len(find) // 4))  # ignore short coincidental runs in the margins
    runs = [op for op in Indel.opcodes(find, window) if op.tag == "equal" and op.dest_end - op.dest_start >= shortest]
    if not runs:
        return start, end
    start, before = offset + runs[0].dest_start, runs[0].src_start
    while before > 0 and start > 0 and document[start - 1] == find[before - 1]:
        start, before = start - 1, before - 1
    end, after = offset + runs[-1].dest_end, runs[-1].src_end
    while after < len(find) and end < len(document) and document[end] == find[after]:
        end, after = end + 1, after + 1
    return start, end


def apply(document: str, match: Match, replace: str) -> str:
    return document[: match.start] + replace + document[match.end :]
