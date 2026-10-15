"""Preregistered design parameters. Changing anything here after data collection starts is a deviation."""

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = REPO_ROOT / "documents"

ROUNDS = 20
REPLICATES = 5
EFFORT = "high"  # passed to `claude -p --effort` for both editors and graders

# Editors and graders are the same three models (graders are blinded to which model made an edit).
MODELS = {
    "opus-5.5": "claude-opus-5-5",
    "sonnet-5.5": "claude-sonnet-5-5",
    "haiku-5.5": "claude-haiku-5-5",
}
GRADERS = list(MODELS)  # order is the tie-break priority for consensus coding

# A FIND that doesn't match exactly gets one retry; after that the round is recorded as failed
# and the document carries forward unchanged.
MAX_EDIT_ATTEMPTS = 2
MAX_GRADER_ATTEMPTS = 3
FUZZY_MATCH_THRESHOLD = 95.0  # rapidfuzz partial_ratio, 0-100
GRADER_CONTEXT_CHARS = 1500  # characters of document shown to graders on each side of an edit

SHUFFLE_SEED = 20261014
PERMUTATION_SEED = 20261014
PERMUTATIONS = 20_000


@dataclass(frozen=True)
class Condition:
    id: str
    second_person: bool = False  # "your constitution ... shapes how you behave"
    allow_no_edit: bool = False  # model may answer [NO CHANGE]
    prepend_persona: bool = True  # prepend the model's claude.ai system prompt (constitution only)
    implementation_note: bool = False  # "any change you propose will be implemented..."


CONDITIONS = {
    c.id: c
    for c in [
        Condition("baseline"),
        Condition("you_framing", second_person=True),
        Condition("allow_no_edit", allow_no_edit=True),
        Condition("no_system_prompt", prepend_persona=False),
        Condition("implementation_note", implementation_note=True),
    ]
}


@dataclass(frozen=True)
class Document:
    id: str
    kind: str  # "constitution" or "system prompt", as it appears in the user message
    provider: str

    def path(self, model_id: str) -> Path:
        if self.id == "claude_constitution":
            return DOCUMENTS_DIR / "claude_constitution.md"
        return DOCUMENTS_DIR / "system_prompts" / f"{model_id}.md"

    def name(self, model_id: str) -> str:
        if self.id == "claude_constitution":
            return "Claude's Constitution"
        return f"{display_name(model_id)} System Prompt"


DOCUMENTS = {
    "claude_constitution": Document("claude_constitution", "constitution", "Anthropic"),
    "claude_system_prompt": Document("claude_system_prompt", "system prompt", "Anthropic"),
}


def display_name(model_id: str) -> str:
    """'claude-opus-5-5' -> 'Claude Opus 5.5'"""
    _, family, major, minor = model_id.split("-")
    return f"Claude {family.title()} {major}.{minor}"


def cell_is_run(document_id: str, condition_id: str) -> bool:
    # The persona is never prepended when the document *is* the system prompt, so
    # no_system_prompt would duplicate baseline there.
    return not (document_id == "claude_system_prompt" and condition_id == "no_system_prompt")
