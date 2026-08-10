"""
Orchestration agent — wraps date extraction -> LLM intake -> enrichment ->
validation into one explicit state machine.

Deliberately hand-rolled rather than a generic agent framework (LangGraph
etc.) — per §7's "duidelijke states and transitions, een maximum aantal
iteraties, gedrag bij een failed agent", the point is that these are visible
and inspectable, not hidden inside a framework's internals.

Only ONE step is nondeterministic (the LLM intake call) — that's the only
one with a retry cap. Every other step is deterministic: if it fails, it
fails because of a real bug or bad input, and retrying won't help, so it
fails immediately with a clear, logged reason instead of silently retrying
something that can't succeed differently the second time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

try:
    # Real repo layout: orchestrator.py lives in App/agents/, imported as
    # part of the App/ package (App/ sits at sys.path[0] because main.py
    # lives there) — so every cross-folder import needs its subfolder prefix.
    from schema.schema import BookingIntakeResult, ValidationStatus
    from agents.validation_agent import ValidationAgent
    from enrichment.enrichment import extract_message_date, enrich
    from agents.intake_agent import run_intake_agent
except ImportError:
    # Fallback for running this file standalone from inside App/agents/
    # (e.g. `python orchestrator.py` for a quick check) — same-directory
    # siblings resolve flat, but schema/enrichment are still one level up
    # and one folder over, so this fallback only covers the common case of
    # testing orchestrator.py + its same-folder siblings in isolation.
    from validation_agent import ValidationAgent  # type: ignore
    from intake_agent import run_intake_agent  # type: ignore
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # App/
    from schema.schema import BookingIntakeResult, ValidationStatus  # type: ignore
    from enrichment.enrichment import extract_message_date, enrich  # type: ignore


class OrchestrationState(str, Enum):
    RECEIVED = "received"
    DATE_EXTRACTED = "date_extracted"
    INTAKE_EXTRACTED = "intake_extracted"
    ENRICHED = "enriched"
    VALIDATED = "validated"
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"  # system-level failure — pipeline could not complete at all,
                        # distinct from BLOCKED (pipeline completed, but the
                        # *content* has a problem the validation agent found)


@dataclass
class StateTransition:
    state: OrchestrationState
    timestamp: str
    detail: str = ""


@dataclass
class OrchestrationResult:
    message_id: str
    success: bool
    result: BookingIntakeResult | None
    final_state: OrchestrationState
    history: list[StateTransition] = field(default_factory=list)
    error: str | None = None

    def history_as_dicts(self) -> list[dict]:
        return [{"state": t.state.value, "timestamp": t.timestamp, "detail": t.detail} for t in self.history]


class BookingOrchestrator:
    """
    Usage:
        orch = BookingOrchestrator(db, memory_db)
        outcome = orch.run("M-001", raw_email_text)
        if outcome.success:
            print(outcome.result.model_dump_json(indent=2))
        else:
            print("failed:", outcome.error)
    """

    MAX_LLM_RETRIES = 2            # total attempts = 1 + this
    LLM_RETRY_BACKOFF_SECONDS = 1.0

    def __init__(self, db, memory_db, log_path: str | None = None, intake_fn=None):
        self.db = db
        self.memory_db = memory_db  # the agent's own derived-state store,
        # separate from db (curated reference fixtures) — see agent_memory.py
        self.log_path = log_path  # if set, appends one JSON line per state transition
        # Injectable for testing — avoids the classic "patch where it's used,
        # not where it's defined" mock gotcha entirely. Defaults to the real
        # LLM call; tests pass a stub function directly instead of patching
        # module globals (which breaks depending on whether this module is
        # run as __main__ or imported elsewhere).
        self.intake_fn = intake_fn or run_intake_agent

    def run(self, message_id: str, raw_email: str) -> OrchestrationResult:
        history: list[StateTransition] = []

        def transition(state: OrchestrationState, detail: str = "") -> None:
            entry = StateTransition(state=state, timestamp=datetime.utcnow().isoformat() + "Z", detail=detail)
            history.append(entry)
            self._log(message_id, entry)

        transition(OrchestrationState.RECEIVED)

        # --- 1. Date extraction — deterministic, no retry helps a bad/missing header ---
        message_date = extract_message_date(raw_email)
        if message_date is None:
            transition(OrchestrationState.FAILED, "kon Datum: header niet vinden of parsen")
            return OrchestrationResult(
                message_id=message_id, success=False, result=None,
                final_state=OrchestrationState.FAILED, history=history,
                error="could not extract message date from 'Datum:' header",
            )
        transition(OrchestrationState.DATE_EXTRACTED)

        # --- 2. LLM intake extraction — the ONE nondeterministic step, capped retries ---
        llm_start = time.perf_counter()
        intake_outcome, llm_error, attempts = self._run_intake_with_retry(raw_email)
        llm_latency_ms = int((time.perf_counter() - llm_start) * 1000)  # total across all attempts

        if intake_outcome is None:
            transition(OrchestrationState.FAILED, f"LLM intake mislukt na {attempts} poging(en): {llm_error}")
            return OrchestrationResult(
                message_id=message_id, success=False, result=None,
                final_state=OrchestrationState.FAILED, history=history,
                error=f"intake extraction failed after {attempts} attempt(s): {llm_error}",
            )
        transition(OrchestrationState.INTAKE_EXTRACTED, f"{attempts} poging(en)")

        # --- 3. Enrichment — deterministic; a failure here means a real bug ---
        try:
            result = enrich(
                self.db, self.memory_db, message_id=message_id, intake=intake_outcome["parsed"],
                message_date=message_date, llm_calls=attempts, latency_ms=llm_latency_ms,
                model=intake_outcome["model"],
            )
        except Exception as exc:
            transition(OrchestrationState.FAILED, f"enrichment fout: {exc}")
            return OrchestrationResult(
                message_id=message_id, success=False, result=None,
                final_state=OrchestrationState.FAILED, history=history,
                error=f"enrichment failed: {exc}",
            )
        transition(OrchestrationState.ENRICHED)

        # --- 4. Validation — deterministic; still guarded, since a bug here
        # shouldn't take down the whole run either ---
        try:
            validation = ValidationAgent(result, raw_email=raw_email).run()
            result.validation = validation
        except Exception as exc:
            transition(OrchestrationState.FAILED, f"validatie fout: {exc}")
            return OrchestrationResult(
                message_id=message_id, success=False, result=result,
                final_state=OrchestrationState.FAILED, history=history,
                error=f"validation failed: {exc}",
            )
        transition(OrchestrationState.VALIDATED)

        final_state = {
            ValidationStatus.ready: OrchestrationState.READY,
            ValidationStatus.needs_review: OrchestrationState.NEEDS_REVIEW,
            ValidationStatus.blocked: OrchestrationState.BLOCKED,
        }[validation.status]
        transition(final_state)

        return OrchestrationResult(
            message_id=message_id, success=True, result=result,
            final_state=final_state, history=history, error=None,
        )

    # -------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------

    def _run_intake_with_retry(self, raw_email: str) -> tuple[dict | None, str | None, int]:
        """Returns (outcome, last_error, attempts_made). outcome is None if
        every attempt failed."""
        last_error = None
        for attempt in range(1, self.MAX_LLM_RETRIES + 2):  # +2: 1-indexed, inclusive of the retries
            try:
                outcome = self.intake_fn(raw_email)
            except Exception as exc:  # network error, API error, timeout, etc.
                last_error = str(exc)
            else:
                if outcome["error"] is None:
                    return outcome, None, attempt
                last_error = outcome["error"]

            if attempt <= self.MAX_LLM_RETRIES:
                time.sleep(self.LLM_RETRY_BACKOFF_SECONDS)

        return None, last_error, self.MAX_LLM_RETRIES + 1

    def _log(self, message_id: str, entry: StateTransition) -> None:
        if not self.log_path:
            return
        line = json.dumps({
            "message_id": message_id, "state": entry.state.value,
            "timestamp": entry.timestamp, "detail": entry.detail,
        })
        with open(self.log_path, "a") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    from tinydb import TinyDB
    # This file lives at App/agents/orchestrator.py — Db/db.json is two
    # levels up (App/agents/ -> App/ -> project root -> Db/).
    from pathlib import Path
    import tempfile
    db_path = Path(__file__).resolve().parent.parent.parent / "Db" / "db.json"
    db = TinyDB(str(db_path))

    # Throwaway memory store for this smoke test — not the real
    # outputs/agent_memory.json, so repeated test runs don't accumulate state.
    _tmpdir = tempfile.TemporaryDirectory()
    memory_db = TinyDB(Path(_tmpdir.name) / "agent_memory.json")

    NO_DATE_EMAIL = "Onderwerp: test\n\nGeen datum header hier."
    WITH_DATE_EMAIL = "Datum: 17 juni 2026\nOnderwerp: test\n\nInhoud."

    import time as _time_module
    _time_module.sleep = lambda *_: None  # don't actually wait through retry backoffs during tests

    # --- Test 1: missing date header fails immediately, no LLM call attempted ---
    call_log = []

    def tracking_stub(raw_email):
        call_log.append(raw_email)
        return {"parsed": "should never be reached", "error": None, "raw": None,
                "prompt_version": "intake-v1", "model": "test-model"}

    orch = BookingOrchestrator(db, memory_db, intake_fn=tracking_stub)
    outcome = orch.run("T-001", NO_DATE_EMAIL)
    assert not outcome.success
    assert outcome.final_state == OrchestrationState.FAILED
    assert "Datum" in outcome.error
    assert call_log == [], "date extraction failure should short-circuit before any LLM call"
    print("test 1 (missing date) passed:", outcome.final_state, "-", outcome.error)

    # --- Test 2: LLM fails twice, succeeds on the 3rd attempt (within MAX_LLM_RETRIES=2) ---
    call_count = {"n": 0}

    def flaky_stub(raw_email):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return {"parsed": None, "error": "simulated transient failure", "raw": None,
                     "prompt_version": "intake-v1", "model": "test-model"}
        # NOTE: a real successful outcome needs a real IntakeExtraction object
        # for enrich() to succeed — this stub will fail at the enrichment
        # step without full deps installed. That's fine here: this test only
        # asserts RETRY behavior, calling _run_intake_with_retry directly
        # rather than the full run().
        return {"parsed": "STUB_NOT_A_REAL_INTAKE_EXTRACTION", "error": None, "raw": None,
                 "prompt_version": "intake-v1", "model": "test-model"}

    orch2 = BookingOrchestrator(db, memory_db, intake_fn=flaky_stub)
    outcome2, last_error, attempts = orch2._run_intake_with_retry(WITH_DATE_EMAIL)
    assert attempts == 3, attempts
    assert outcome2 is not None and outcome2["error"] is None
    print("test 2 (retry then succeed) passed: attempts =", attempts)

    # --- Test 3: LLM fails every attempt, exhausts the retry cap ---
    def always_fails_stub(raw_email):
        return {"parsed": None, "error": "permanent failure", "raw": None,
                 "prompt_version": "intake-v1", "model": "test-model"}

    orch3 = BookingOrchestrator(db, memory_db, intake_fn=always_fails_stub)
    outcome3 = orch3.run("T-003", WITH_DATE_EMAIL)
    assert not outcome3.success
    assert outcome3.final_state == OrchestrationState.FAILED
    assert f"{BookingOrchestrator.MAX_LLM_RETRIES + 1} attempt" in outcome3.error
    print("test 3 (exhausted retries) passed:", outcome3.error)

    _tmpdir.cleanup()

    print("\nNOTE: full success-path test (real LLM -> enrich -> validate) needs "
          "a real IntakeExtraction-returning intake_fn (e.g. the real "
          "run_intake_agent with OPENROUTER_API_KEY set) — run against a real "
          "sample email separately, e.g.:\n"
          "    orch = BookingOrchestrator(db, memory_db)  # uses real run_intake_agent by default\n"
          "    outcome = orch.run('M-001', real_email_text)")
