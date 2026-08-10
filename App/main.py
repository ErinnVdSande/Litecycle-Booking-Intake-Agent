"""
CLI entrypoint — runs one or more emails through the full pipeline
(date extraction -> LLM intake -> enrichment -> validation) via
BookingOrchestrator, and writes the result JSON for each to disk.

Actual repo layout (this file lives INSIDE App/, per tree.txt):
    App/
        main.py             <- this file
        agents/
            intake_agent.py
            orchestrator.py
            validation_agent.py
        enrichment/
            enrichment.py
        matchers/
            gazetteer.py
            precedentMatcher.py
            voyageMatcher.py
        schema/
            schema.py
    Db/
        db.json
    Examples/
        sample1.txt ... sample5.txt
    Notebook/
        booking_intake_agent_notebook.ipynb

main.py does NOT need to add App/ to sys.path — Python automatically puts
the running script's own directory (App/) at sys.path[0], which is exactly
what makes `agents`, `enrichment`, `matchers`, `schema` importable as
namespace packages (e.g. `from matchers.gazetteer import ...`). Db/ and
Examples/ are one level UP from App/, so they're addressed as
`Path(__file__).parent.parent / "Db"` etc.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent

from dotenv import load_dotenv  # noqa: E402

load_dotenv(APP_DIR / ".env")

from tinydb import TinyDB  # noqa: E402
from agents.orchestrator import BookingOrchestrator, OrchestrationState  # noqa: E402


def load_emails(examples_dir: Path) -> dict[str, str]:
    """Examples/sample1.txt -> message_id 'sample1'. Rename files to
    M-001.txt etc. if you want the assignment's own message IDs instead."""
    files = sorted(examples_dir.glob("*.txt"))
    if not files:
        raise FileNotFoundError(
            f"No .txt files found in {examples_dir} — add the sample emails there first."
        )
    return {f.stem: f.read_text(encoding="utf-8") for f in files}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run emails through the booking intake pipeline.")
    parser.add_argument("--examples-dir", default=str(PROJECT_ROOT / "Examples"))
    parser.add_argument("--db", default=str(PROJECT_ROOT / "Db" / "db.json"))
    parser.add_argument("--outputs-dir", default=str(PROJECT_ROOT / "outputs"))
    parser.add_argument("--memory-db", default=None,
                         help="Path to the agent's own derived-state store (defaults to "
                              "<outputs-dir>/agent_memory.json). Separate from --db, which is "
                              "curated reference data — delete this file freely to reset agent "
                              "state without touching the master data fixtures.")
    parser.add_argument("--log-path", default=str(PROJECT_ROOT / "outputs" / "run_log.jsonl"),
                         help="JSONL log of state transitions per message. Pass '' to disable.")
    args = parser.parse_args()

    examples_dir = Path(args.examples_dir)
    outputs_dir = Path(args.outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    db = TinyDB(args.db)
    memory_db_path = args.memory_db or str(outputs_dir / "agent_memory.json")
    memory_db = TinyDB(memory_db_path)
    log_path = args.log_path or None
    orchestrator = BookingOrchestrator(db, memory_db, log_path=log_path)

    emails = load_emails(examples_dir)

    results = []
    for message_id, email_text in emails.items():
        print(f"Processing {message_id}...")
        outcome = orchestrator.run(message_id, email_text)
        results.append(outcome)

        if outcome.success:
            out_path = outputs_dir / f"{message_id}.json"
            out_path.write_text(outcome.result.model_dump_json(indent=2), encoding="utf-8")
            print(f"  -> {outcome.final_state.value} — written to {out_path}")
        else:
            out_path = outputs_dir / f"{message_id}.error.json"
            out_path.write_text(json.dumps({
                "message_id": message_id,
                "error": outcome.error,
                "history": outcome.history_as_dicts(),
            }, indent=2), encoding="utf-8")
            print(f"  -> FAILED — {outcome.error} (details in {out_path})")

    print("\nSummary:")
    print(f"{'message_id':<15} {'final_state':<15} error")
    print("-" * 60)
    for outcome in results:
        error = outcome.error or ""
        print(f"{outcome.message_id:<15} {outcome.final_state.value:<15} {error}")

    n_success = sum(1 for r in results if r.success)
    print(f"\n{n_success}/{len(results)} processed successfully.")
    if log_path:
        print(f"State-transition log: {log_path}")
    print(f"Agent memory: {memory_db_path}")


if __name__ == "__main__":
    main()
