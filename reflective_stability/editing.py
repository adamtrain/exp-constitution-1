"""Stage 1: each trajectory is ROUNDS consecutive single-edit rounds on one evolving document.

Every round appends one JSON line to the trajectory's file. A trajectory's document is never
stored; it is rebuilt by replaying the recorded edits on the frozen original (hashes are checked
at every step), which makes interrupted runs resumable and the record self-verifying.
"""

import hashlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import claude_cli, edits, prompts
from .config import (
    CONDITIONS,
    DOCUMENTS,
    EFFORT,
    MAX_EDIT_ATTEMPTS,
    MODELS,
    REPLICATES,
    ROUNDS,
    SHUFFLE_SEED,
    cell_is_run,
)
from .documents import load_document, load_persona
from .runtime import STOP, Progress, Stopped, append_jsonl, read_jsonl, run_parallel, say


@dataclass(frozen=True)
class Trajectory:
    condition: str
    document: str
    model: str  # key into MODELS
    replicate: int

    @property
    def id(self) -> str:
        return f"{self.condition}/{self.document}/{self.model}/rep{self.replicate:02d}"

    @property
    def model_id(self) -> str:
        return MODELS[self.model]

    def path(self, data_dir: Path) -> Path:
        return data_dir / "edits" / f"{self.id}.jsonl"


def plan(rounds: int = ROUNDS, replicates: int = REPLICATES, models=None, conditions=None, documents=None):
    """All trajectories in the design, in a fixed shuffled order so no cell runs systematically first."""
    trajectories = [
        Trajectory(condition, document, model, replicate)
        for condition in conditions or CONDITIONS
        for document in documents or DOCUMENTS
        for model in models or MODELS
        for replicate in range(1, replicates + 1)
        if cell_is_run(document, condition)
    ]
    random.Random(SHUFFLE_SEED).shuffle(trajectories)
    # Replicate 1 of every cell first, then replicate 2, ... so partial data stays balanced.
    return sorted(trajectories, key=lambda t: t.replicate)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_records(trajectory: Trajectory, data_dir: Path) -> list[dict]:
    return read_jsonl(trajectory.path(data_dir))


def replay(trajectory: Trajectory, records: list[dict]) -> tuple[list[str], str]:
    """Rebuild the document each round's model saw, plus the current document, verifying every hash."""
    document = load_document(trajectory.document, trajectory.model_id)
    seen = []
    for record in records:
        if record["doc_sha256_before"] != sha256(document):
            raise RuntimeError(f"{trajectory.id} round {record['round']}: document does not match its record")
        seen.append(document)
        if record["status"] == "applied":
            start, end = record["span"]
            document = document[:start] + record["replace"] + document[end:]
        if record["doc_sha256_after"] != sha256(document):
            raise RuntimeError(f"{trajectory.id} round {record['round']}: replayed edit does not match its record")
    return seen, document


def run_trajectory(trajectory: Trajectory, data_dir: Path, rounds: int, progress: Progress) -> None:
    path = trajectory.path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = read_records(trajectory, data_dir)
    _, document = replay(trajectory, records)

    condition = CONDITIONS[trajectory.condition]
    doc_spec = DOCUMENTS[trajectory.document]
    system_prompt = prompts.edit_system_prompt(doc_spec, condition, load_persona(trajectory.model_id))

    for round_number in range(len(records) + 1, rounds + 1):
        if STOP.is_set():
            raise Stopped
        user_message = prompts.edit_user_message(doc_spec, condition, trajectory.model_id, document)
        record = edit_once(trajectory, round_number, system_prompt, user_message, document)
        append_jsonl(path, record)
        if record["status"] == "applied":
            start, end = record["span"]
            document = document[:start] + record["replace"] + document[end:]
        progress.advance(describe(record, rounds), sum(a["cost_usd"] for a in record["attempts"]))


def describe(record: dict, rounds: int) -> str:
    """One readable progress line for a finished round."""
    attempts = record["attempts"]
    seconds = sum(a["duration_ms"] for a in attempts) / 1000
    if record["status"] == "applied":
        start, end = record["span"]
        outcome = f"applied ({record['match_strategy']}, -{end - start}/+{len(record['replace'])} chars)"
    elif record["status"] == "no_change":
        outcome = "[NO CHANGE]"
    else:
        outcome = f"FAILED ({attempts[-1]['error'][:80]})"
    retry = ", retried" if len(attempts) > 1 else ""
    summary = (record["change_description"] or "").replace("\n", " ")
    summary = f' "{summary[:90]}…"' if len(summary) > 90 else f' "{summary}"' if summary else ""
    return f"{record['trajectory']} round {record['round']}/{rounds}: {outcome}{retry}, {seconds:.0f}s{summary}"


def edit_once(trajectory: Trajectory, round_number: int, system_prompt: str, user_message: str, document: str):
    """Ask for one edit; on a malformed or unmatchable answer, retry once with the error explained."""
    condition = CONDITIONS[trajectory.condition]
    attempts = []
    record = {
        "edit_id": f"{trajectory.id}/round{round_number:02d}",
        "trajectory": trajectory.id,
        "condition": trajectory.condition,
        "document": trajectory.document,
        "model": trajectory.model,
        "model_id": trajectory.model_id,
        "replicate": trajectory.replicate,
        "round": round_number,
        "doc_sha256_before": sha256(document),
        "status": "failed",
        "change_description": None,
        "find": None,
        "replace": None,
        "span": None,
        "match_strategy": None,
        "match_score": None,
        "attempts": attempts,
    }
    message = user_message
    for _ in range(MAX_EDIT_ATTEMPTS):
        reply = claude_cli.ask(trajectory.model_id, system_prompt, message, EFFORT)
        attempt = {
            "response": reply.text,
            "error": None,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cost_usd": reply.cost_usd,
            "duration_ms": reply.duration_ms,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        attempts.append(attempt)
        try:
            proposed = edits.parse_response(reply.text)
            if proposed.is_no_change:
                if not condition.allow_no_edit:
                    raise edits.EditError(f"{edits.NO_CHANGE} is not an option here; make one revision.")
                record.update(status="no_change", change_description=proposed.description)
                break
            match = edits.locate(document, proposed.find)
        except edits.EditError as error:
            attempt["error"] = str(error)
            message = f"{user_message}\n\n{prompts.RETRY_NOTE.format(error=error)}"
            continue
        document = edits.apply(document, match, proposed.replace)
        record.update(
            status="applied",
            change_description=proposed.description,
            find=proposed.find,
            replace=proposed.replace,
            span=[match.start, match.end],
            match_strategy=match.strategy,
            match_score=match.score,
        )
        break
    record["doc_sha256_after"] = sha256(document)
    return record


def run(trajectories: list[Trajectory], data_dir: Path, rounds: int, parallel: int) -> None:
    done = {t: len(read_records(t, data_dir)) for t in trajectories}
    pending = [t for t in trajectories if done[t] < rounds]
    spent = sum(a["cost_usd"] for t in trajectories for r in read_records(t, data_dir) for a in r["attempts"])
    say(
        f"EDITING: {len(trajectories) - len(pending)}/{len(trajectories)} trajectories complete, "
        f"{sum(min(n, rounds) for n in done.values())}/{len(trajectories) * rounds} rounds done, "
        f"${spent:,.2f} spent so far. {len(pending)} trajectories to run, {parallel} at a time."
    )
    if not pending:
        return
    progress = Progress("edit", len(trajectories) * rounds, sum(min(n, rounds) for n in done.values()), spent)
    run_parallel(pending, lambda t: run_trajectory(t, data_dir, rounds, progress), parallel)
    say("EDITING finished.")
