"""
Agent memory — the agent's OWN derived state, kept deliberately separate
from Db/db.json (curated SEAFLOW reference fixtures: ports, services,
commodities, relations, voyages, historical bookings).

Why separate, not just another table in db.json:
  - Db/db.json is read-mostly reference data, checked into git, seeded once.
  - This store is write-on-every-run, generated at runtime, and should be
    freely resettable (delete the file, start clean) without any risk to
    the master data fixtures.
  - Lives in outputs/ specifically because that folder is already
    .gitignore'd — no new git config needed, and it's naturally grouped
    with the other runtime-generated artifacts (per-message result JSON,
    run_log.jsonl).

What it stores: one record per successfully processed booking_request,
keyed primarily by customer_reference (known immediately, from the first
message) and secondarily by carrier_booking_no (usually NOT known until a
later message reveals it — e.g. M-003's subject line "Booking no 4471902 //
Your reference DF-026-00604"). A bl_instruction referencing a
carrier_booking_no can then look up what THIS agent already derived for
that booking (specific voyage, commodity, payable, etc.) rather than
falling back to the static booking table, which only has historical/
pre-existing bookings the agent itself never processed.
"""

from __future__ import annotations

from datetime import datetime, timezone


TABLE_NAME = "processed_bookings"


def upsert_processed_booking(
    memory_db,
    customer_reference: str,
    message_id: str,
    record: dict,
    carrier_booking_no: str | None = None,
) -> None:
    """
    Stores/updates what the agent derived for a processed booking_request.
    Keyed by customer_reference (always required — it's known from the
    first message). carrier_booking_no is optional at write time, since
    it's usually not known yet when the booking_request itself is processed.

    `record` is a plain dict of whatever fields are worth remembering —
    see enrichment.py's call site for the actual shape used in production.
    """
    from tinydb import Query
    table = memory_db.table(TABLE_NAME)
    Q = Query()

    payload = {
        "customer_reference": customer_reference,
        "carrier_booking_no": carrier_booking_no,
        "message_id": message_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **record,
    }

    existing = table.search(Q.customer_reference == customer_reference)
    if existing:
        table.update(payload, Q.customer_reference == customer_reference)
    else:
        table.insert(payload)


def backfill_carrier_booking_no(memory_db, customer_reference: str, carrier_booking_no: str) -> bool:
    """
    Called when a later message (e.g. an 'other'-classified reconciliation
    note) reveals the carrier_booking_no for a customer_reference we
    already have a record for. Returns True if a matching record was found
    and updated, False if there was nothing to backfill (e.g. the original
    booking_request was never processed by this agent — a message this
    agent has no memory of).
    """
    from tinydb import Query
    table = memory_db.table(TABLE_NAME)
    Q = Query()

    existing = table.search(Q.customer_reference == customer_reference)
    if not existing:
        return False

    table.update(
        {"carrier_booking_no": carrier_booking_no,
         "updated_at": datetime.now(timezone.utc).isoformat()},
        Q.customer_reference == customer_reference,
    )
    return True


def lookup_by_carrier_booking_no(memory_db, carrier_booking_no: str) -> dict | None:
    from tinydb import Query
    table = memory_db.table(TABLE_NAME)
    Q = Query()
    results = table.search(Q.carrier_booking_no == carrier_booking_no)
    return results[0] if results else None


def lookup_by_customer_reference(memory_db, customer_reference: str) -> dict | None:
    from tinydb import Query
    table = memory_db.table(TABLE_NAME)
    Q = Query()
    results = table.search(Q.customer_reference == customer_reference)
    return results[0] if results else None


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    from tinydb import TinyDB

    with tempfile.TemporaryDirectory() as tmp:
        memory_db = TinyDB(Path(tmp) / "agent_memory.json")

        # M-001 processed: customer_reference known, carrier_booking_no NOT yet known
        upsert_processed_booking(
            memory_db, customer_reference="DF-026-00604", message_id="M-001",
            record={
                "pol_code": "BEGNE", "pod_code": "GYGEO",
                "selected_voyage": {"voyage_code": "CX2614", "vessel": "CORAL TRADER"},
            },
        )
        assert lookup_by_carrier_booking_no(memory_db, "4471902") is None, "not backfilled yet"
        assert lookup_by_customer_reference(memory_db, "DF-026-00604") is not None

        # M-003 arrives, reveals the mapping
        backfilled = backfill_carrier_booking_no(memory_db, "DF-026-00604", "4471902")
        assert backfilled is True

        # M-004 (bl_instruction) can now find it by carrier_booking_no
        record = lookup_by_carrier_booking_no(memory_db, "4471902")
        assert record is not None
        assert record["selected_voyage"]["voyage_code"] == "CX2614"
        print("record found via carrier_booking_no after backfill:", record)

        # backfill against an unknown customer_reference should fail cleanly
        assert backfill_carrier_booking_no(memory_db, "UNKNOWN-REF", "9999999") is False

        print("all checks passed")
