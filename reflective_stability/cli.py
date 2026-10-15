"""Command line entry point: `uv run experiment [command]`.

  (no command)      run everything not yet done: edit -> code -> analyze
  status            show progress so far (safe to run in another terminal during a run)
  plan              show the design and how many calls it needs, without calling anything
  fetch-documents   download and freeze the source documents (done once, before preregistration)
  edit | code | analyze   run a single stage

--pilot runs any command at smoke-test scale (baseline only, 1 replicate, 2 rounds) in
data-pilot/ and results-pilot/. Every stage is resumable: rerunning skips work already saved.
Ctrl-C once stops after the calls in flight finish; twice stops immediately.
"""

import argparse
import fcntl
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import analysis, claude_cli, coding, documents, editing
from .config import CONDITIONS, DOCUMENTS, GRADERS, MODELS, REPO_ROOT, ROUNDS
from .runtime import exit_with, read_jsonl, say


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="experiment", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "status", "plan", "fetch-documents", "edit", "code", "analyze"],
    )
    parser.add_argument("--pilot", action="store_true", help="smoke-test scale, in data-pilot/ and results-pilot/")
    parser.add_argument("--parallel", type=int, default=4, help="concurrent `claude -p` calls (default 4)")
    args = parser.parse_args()

    if args.command == "fetch-documents":
        documents.fetch_all()
        say("Frozen documents written to documents/ (see documents/manifest.json).")
        return

    documents.verify()
    if args.pilot:
        trajectories = editing.plan(replicates=1, conditions=["baseline"])
        data_dir, results_dir, rounds = REPO_ROOT / "data-pilot", REPO_ROOT / "results-pilot", 2
    else:
        trajectories = editing.plan()
        data_dir, results_dir, rounds = REPO_ROOT / "data", REPO_ROOT / "results", ROUNDS

    if args.command == "plan":
        return show_plan(trajectories, rounds)
    if args.command == "status":
        return show_status(trajectories, data_dir, rounds)

    data_dir.mkdir(exist_ok=True)
    lock = (data_dir / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        exit_with(f"Another run is already using {data_dir.name}/. Use `uv run experiment status` to watch it.")
    log_invocation(data_dir, args.command)

    scale = "PILOT" if args.pilot else "FULL EXPERIMENT"
    say(
        f"{scale}: {len(trajectories)} trajectories x {rounds} rounds, {len(GRADERS)} graders, "
        f"{args.parallel} parallel calls. Data in {data_dir.name}/, results in {results_dir.name}/."
    )
    say("Ctrl-C once to stop after the calls in flight finish (nothing lost); rerun the same command to resume.")
    try:
        if args.command in ("run", "edit"):
            editing.run(trajectories, data_dir, rounds, args.parallel)
        if args.command in ("run", "code"):
            coding.run(trajectories, data_dir, args.parallel)
        if args.command in ("run", "analyze"):
            analysis.run(trajectories, data_dir, results_dir, rounds)
    except (claude_cli.FatalError, claude_cli.TransportError) as error:
        exit_with(f"STOPPED: {error}\nFix the cause, then rerun the same command; finished work is kept.")
    except KeyboardInterrupt:
        exit_with("STOPPED by Ctrl-C. Finished work is saved; rerun the same command to resume.")


def show_plan(trajectories, rounds) -> None:
    edit_calls = len(trajectories) * rounds
    replicates = max(t.replicate for t in trajectories)
    say(f"{len(trajectories)} trajectories x {rounds} rounds = {edit_calls} edit calls (plus retries),")
    say(f"then up to {edit_calls * len(GRADERS)} grader calls. Replicates per cell: {replicates}.")


def show_status(trajectories, data_dir: Path, rounds: int) -> None:
    """Summarise what is on disk: rounds per cell, edit outcomes, grading, spend, last activity."""
    if not data_dir.exists():
        return say(f"No data yet in {data_dir.name}/.")
    done, outcomes, spent, applied = Counter(), Counter(), 0.0, 0
    for t in trajectories:
        records = editing.read_records(t, data_dir)[:rounds]
        done[(t.document, t.condition, t.model)] += len(records)
        for r in records:
            outcomes[r["status"]] += 1
            spent += sum(a["cost_usd"] for a in r["attempts"])
            applied += r["status"] == "applied"
    graded = {g: read_jsonl(coding.codes_path(data_dir, g)) for g in GRADERS}
    grading_spent = sum(e.get("cost_usd", 0.0) for entries in graded.values() for e in entries)
    total_rounds = len(trajectories) * rounds

    say(f"Progress in {data_dir.name}/ (rounds done / planned per cell):")
    print(f"  {'document':22} {'condition':20} " + " ".join(f"{m:>12}" for m in MODELS))
    per_cell = Counter((t.document, t.condition, t.model) for t in trajectories)
    for document in DOCUMENTS:
        for condition in CONDITIONS:
            if not any(per_cell[(document, condition, m)] for m in MODELS):
                continue
            cells = [
                f"{done[(document, condition, m)]:>5}/{per_cell[(document, condition, m)] * rounds:<6}" for m in MODELS
            ]
            print(f"  {document:22} {condition:20} " + " ".join(cells))
    print(
        f"\n  Editing: {sum(done.values())}/{total_rounds} rounds "
        f"({', '.join(f'{k} {v}' for k, v in outcomes.most_common()) or 'none yet'}), ${spent:,.2f}"
    )
    print(
        "  Grading: "
        + ", ".join(f"{g} {len(entries)}/{applied}" for g, entries in graded.items())
        + f" applied edits coded, ${grading_spent:,.2f}"
    )
    newest = max((p.stat().st_mtime for p in data_dir.rglob("*.jsonl")), default=None)
    if newest:
        print(f"  Last write: {datetime.fromtimestamp(newest):%a %H:%M:%S}")


def log_invocation(data_dir: Path, command: str) -> None:
    """Append when, how and with which Claude Code version each stage was run."""
    entry = {"started": datetime.now(UTC).isoformat(), "command": command, "claude_cli": claude_cli.cli_version()}
    with (data_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")
