"""
Enrichment pipeline.

Takes the raw LLM extraction (IntakeExtraction) plus the db (TinyDB — master
data, booking history, voyage schedule) and produces the final, validated
BookingIntakeResult.

Two layers:
  - enrich()        pure-ish function: db + plain data in, BookingIntakeResult
                     out. Only I/O is read-only db queries (via the matchers
                     and gazetteer) — no writes, no LLM call — so it's still
                     testable by pointing it at a real or hand-seeded db.
  - process_email()  orchestration layer: extracts the message date from the
                     raw email, calls the LLM intake agent, then calls
                     enrich(). This is the actual pipeline entry point.

Actual repo layout (this file lives at App/enrichment/enrichment.py):
    App/schema/schema.py
    App/matchers/gazetteer.py       (db-backed: build_port_lookup,
                                      build_service_lookup, resolve_port_fuzzy,
                                      resolve_service_fuzzy, resolve_bl_type,
                                      build_relation_lookup, resolve_relation)
    App/agents/intake_agent.py
    App/matchers/precedentMatcher.py
    App/matchers/voyageMatcher.py

Current matcher/gazetteer contracts (confirmed against real code + output):
  match_precedent(db, booker, shipper, pol, pod, goods=None, message_date=None) -> dict,
      ALWAYS a dict, never None. {"booking_no": "NA", "confidence": 0,
      "reason": "Geen precedent gevonden"} is the "nothing found" case — NOT
      booking_no=None. Check `precedent["booking_no"] != "NA"`, not `if precedent:`.
      message_date MUST be passed in production (enrich() does this) — without
      it, a same-day-or-later booking can be matched as its own "precedent".
  match_voyage(db, pol, pod, message_date, required_capacity_t,
               requested_service=None, etd_hint=None) -> dict.
      selected_voyage is the voyage_code STRING (or None), not the full
      record — enrich() looks up the record via _lookup_voyage().
  build_port_lookup(db) / build_service_lookup(db) -> dict, built once per
      call and passed into resolve_port_fuzzy(lookup, raw) /
      resolve_service_fuzzy(lookup, raw). Both return a GazetteerMatch with
      .code, .name, .confidence, .evidence — matching is done on the db's
      port/service tables directly, no hardcoded alias duplicate.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# --- App/ sits at sys.path[0] because main.py lives there; every
# cross-folder import below needs its subfolder prefix -----------------------
try:
    from schema.schema import (
        BLType, BookingIntakeResult, CargoLine, Classification, Commercial,
        DepartureMode, Documentation, InnerPackages, Language, MessageInfo,
        Packages, Parties, Party, PortRef, References, RequestedDeparture,
        Routing, SelectedVoyage, SourceEntry, SourceType, Sources,
        ValidationStatus, WeightBasis, RunMetadata, Validation,
    )
    from matchers.gazetteer import build_port_lookup, resolve_port_fuzzy, resolve_bl_type
    from matchers.gazetteer import build_relation_lookup, resolve_relation
    from agents.intake_agent import IntakeExtraction
    from matchers.precedentMatcher import match_precedent
    from matchers.voyageMatcher import match_voyage
    from memory.agent_memory import (
        upsert_processed_booking, lookup_by_carrier_booking_no, backfill_carrier_booking_no,
    )
except ImportError:
    # Fallback for running this file standalone from inside App/enrichment/
    # — App/ isn't automatically on sys.path in that case, so add it first.
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # App/
    from schema.schema import (  # type: ignore
        BLType, BookingIntakeResult, CargoLine, Classification, Commercial,
        DepartureMode, Documentation, InnerPackages, Language, MessageInfo,
        Packages, Parties, Party, PortRef, References, RequestedDeparture,
        Routing, SelectedVoyage, SourceEntry, SourceType, Sources,
        ValidationStatus, WeightBasis, RunMetadata, Validation,
    )
    from matchers.gazetteer import build_port_lookup, resolve_port_fuzzy, resolve_bl_type  # type: ignore
    from matchers.gazetteer import build_relation_lookup, resolve_relation  # type: ignore
    from agents.intake_agent import IntakeExtraction  # type: ignore
    from matchers.precedentMatcher import match_precedent  # type: ignore
    from matchers.voyageMatcher import match_voyage  # type: ignore
    from memory.agent_memory import (  # type: ignore
        upsert_processed_booking, lookup_by_carrier_booking_no, backfill_carrier_booking_no,
    )


# =============================================================================
# 0. Message date extraction — deterministic, from the "Datum:" header
# =============================================================================

DATE_HEADER_RE = re.compile(r"^Datum:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

NL_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}


def extract_message_date(raw_email: str) -> date | None:
    """
    Pulls the message date from a `Datum:` header. Deterministic, tier-1 —
    no reason to route this through the LLM. Returns None if the header is
    missing or the date format isn't recognized; caller decides how to
    handle that (process_email() below treats it as a hard failure).
    """
    match = DATE_HEADER_RE.search(raw_email)
    if not match:
        return None
    raw = match.group(1).strip()

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # Optional leading weekday name (Dutch: "woensdag 17 juni 2026 08:41") —
    # \w+\s+ is tried greedily first and backtracks cleanly if there's no
    # weekday (e.g. plain "17 juni 2026"), so this one pattern covers both.
    # Trailing time ("08:41") is simply ignored, since re.match doesn't
    # require consuming the whole string.
    m = re.match(r"(?:\w+\s+)?(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if m:
        day, month_name, year = m.groups()
        month = NL_MONTHS.get(month_name.lower())
        if month:
            return date(int(year), month, int(day))

    return None


ETD_HINT_RE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?$")


def parse_etd_hint(raw: str | None, message_date: date) -> date | None:
    """
    Parses an ETD hint like "24/6", "24-6", "24/06/2026" into a real date.
    No year in the input is the common case (e.g. M-002's "ETD 24/6") — the
    message's own year is assumed, with a rollover to next year if that date
    would fall before the message date (a requested departure can't be in
    the past relative to when it was requested).
    """
    if not raw:
        return None
    raw = raw.strip()

    m = ETD_HINT_RE.match(raw)
    if m:
        day, month, year = m.groups()
        day, month = int(day), int(month)
        year = int(year) if year else message_date.year
        if year < 100:  # 2-digit year shorthand
            year += 2000
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None  # e.g. "31/2" — not a real date, don't guess further
        if candidate < message_date and not m.group(3):
            # no explicit year given, and the naive same-year date is in the
            # past relative to the message — assume next year rather than a
            # backdated request
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return None
        return candidate

    # NL month-name form, e.g. "24 juni" (no year)
    m = re.match(r"(\d{1,2})\s+(\w+)$", raw)
    if m:
        day, month_name = m.groups()
        month = NL_MONTHS.get(month_name.lower())
        if month:
            try:
                candidate = date(message_date.year, month, int(day))
            except ValueError:
                return None
            if candidate < message_date:
                try:
                    candidate = date(message_date.year + 1, month, int(day))
                except ValueError:
                    return None
            return candidate

    return None


# =============================================================================
# 1. Gazetteer resolution — db-backed, lookup built once per call
# =============================================================================

def resolve_routing_codes(db, intake: IntakeExtraction) -> tuple[PortRef, PortRef, dict]:
    """Resolve raw port text to codes via the db's port table. Returns (pol, pod, sources_fragment)."""
    port_lookup = build_port_lookup(db)
    sources = {}

    pol_raw = intake.port_of_loading_raw or ""
    pol_match = resolve_port_fuzzy(port_lookup, pol_raw) if pol_raw else None
    pol = PortRef(raw=pol_raw, code=pol_match.code if pol_match else None)
    if pol_match:
        sources["port_of_loading"] = SourceEntry(
            source=SourceType.MASTER_DATA, confidence=pol_match.confidence, evidence=pol_match.evidence
        )

    pod_raw = intake.port_of_discharge_raw or ""
    pod_match = resolve_port_fuzzy(port_lookup, pod_raw) if pod_raw else None
    pod = PortRef(raw=pod_raw, code=pod_match.code if pod_match else None)
    # POD isn't in the 8 sourced fields per the assignment spec, so no sources entry

    return pol, pod, sources


# =============================================================================
# Lookup helpers — both matchers return only an identifier (booking_no /
# voyage_code), not the full record. Enrichment fetches the record itself.
# =============================================================================

def _lookup_booking(db, booking_no: str) -> dict | None:
    from tinydb import Query
    Booking = Query()
    results = db.table("booking").search(Booking.booking_no == booking_no)
    return results[0] if results else None


def _lookup_voyage(db, voyage_code: str) -> dict | None:
    from tinydb import Query
    Voyage = Query()
    results = db.table("voyage").search(Voyage.voyage_code == voyage_code)
    return results[0] if results else None


# =============================================================================
# 2. Cargo line conversion (LLM extraction -> schema CargoLine)
# =============================================================================

def build_cargo_lines(intake: IntakeExtraction, fallback_commodity_code: str | None) -> list[CargoLine]:
    lines = []
    for c in intake.cargo:
        commodity_code = fallback_commodity_code or "999"  # N.O.S. default per §6.3
        lines.append(CargoLine(
            line_no=c.line_no,
            packages=Packages(count=c.packages_count or 0, type=c.packages_type or ""),
            inner_packages=(
                InnerPackages(
                    count=c.inner_packages_count, unit=c.inner_packages_unit or "",
                    description=c.inner_packages_description or "",
                )
                if c.inner_packages_count is not None else None
            ),
            goods_description=c.goods_description,
            gross_weight_kg=c.gross_weight_kg or 0.0,
            weight_basis=WeightBasis.stated if c.gross_weight_kg else WeightBasis.unknown,
            hs_code=(c.hs_code_raw or "").replace(".", "").replace("-", "") or None,
            commodity_code=commodity_code,
        ))
    return lines


def total_weight_tonnes(cargo: list[CargoLine]) -> float:
    return sum(c.gross_weight_kg for c in cargo) / 1000.0


# =============================================================================
# 2b. bl_instruction routing resolution — check our OWN memory first (the
# specific voyage this agent already selected), fall back to the static
# booking table (historical bookings this agent never itself processed).
# Only relevant when the message itself doesn't restate the route, which is
# the normal case for a BL instruction confirming an already-existing booking.
# =============================================================================

VOYAGE_CODE_RE = re.compile(r"\b([A-Z]{2}\d{3,4})\b")


def resolve_bl_instruction_routing(
    db, memory_db, intake: IntakeExtraction,
) -> tuple[PortRef, PortRef, "SelectedVoyage | None", dict] | None:
    carrier_booking_no = intake.carrier_booking_no
    if not carrier_booking_no:
        return None

    # --- 1. Our own memory: richer, since it has the SPECIFIC voyage this
    # agent selected when it originally processed the booking_request. ---
    memory_record = lookup_by_carrier_booking_no(memory_db, carrier_booking_no)
    if memory_record:
        pol = PortRef(raw=memory_record.get('pol_raw') or '', code=memory_record.get('pol_code'))
        pod = PortRef(raw=memory_record.get('pod_raw') or '', code=memory_record.get('pod_code'))
        sv_dict = memory_record.get('selected_voyage')
        selected_voyage = SelectedVoyage(**sv_dict) if sv_dict else None
        sources = {}
        if pol.code:
            # port_of_loading is one of the 8 fields the Sources schema tracks
            # (port_of_discharge isn't — matches the assignment's own §5 example).
            sources['port_of_loading'] = SourceEntry(
                source=SourceType.SYSTEM, confidence=1.0,
                evidence=f"eerder verwerkt door dit systeem (boeking {carrier_booking_no})",
            )
        if selected_voyage:
            sources['selected_voyage'] = SourceEntry(
                source=SourceType.SYSTEM, confidence=1.0,
                evidence=f"eerder verwerkt door dit systeem (boeking {carrier_booking_no})",
            )
        return pol, pod, selected_voyage, sources

    # --- 2. Static booking table fallback: a historical booking this agent
    # never processed itself (only has pol/pod city names, not a specific
    # voyage_code) — matches the assignment's own fixture data for 4471902. ---
    booking_record = _lookup_booking(db, carrier_booking_no)
    if booking_record is None:
        return None  # genuinely unknown booking — let validation flag it

    port_lookup = build_port_lookup(db)
    pol_match = resolve_port_fuzzy(port_lookup, booking_record['pol'])
    pod_match = resolve_port_fuzzy(port_lookup, booking_record['pod'])
    pol = PortRef(raw=booking_record['pol'], code=pol_match.code if pol_match else None)
    pod = PortRef(raw=booking_record['pod'], code=pod_match.code if pod_match else None)
    sources = {}
    if pol.code:
        sources['port_of_loading'] = SourceEntry(
            source=SourceType.MASTER_DATA, confidence=1.0,
            evidence=f"boeking {carrier_booking_no} (historisch, niet eerder verwerkt door dit systeem)",
        )

    # The specific voyage isn't in the booking table, but it's often stated
    # directly in the message itself (e.g. M-004's subject: "CX SERVICE/CX2614"
    # already lands in requested_service_or_vessel_raw via normal extraction) —
    # try to pull a voyage code out of that and look it up.
    selected_voyage = None
    raw_service = intake.requested_service_or_vessel_raw or ""
    m = VOYAGE_CODE_RE.search(raw_service)
    if m:
        voyage_code = m.group(1)
        voyage_record = _lookup_voyage(db, voyage_code)
        if voyage_record:
            selected_voyage = SelectedVoyage(
                voyage_code=voyage_record['voyage_code'], vessel=voyage_record['vessel'],
                ets_pol=voyage_record['ets_pol'], eta_pod=voyage_record['eta_pod'],
                selection_reason=f"afvaart genoemd in bericht, gekoppeld aan boeking {carrier_booking_no}",
                alternatives=[],
            )
            sources['selected_voyage'] = SourceEntry(
                source=SourceType.MESSAGE, confidence=0.9, evidence=voyage_code,
            )

    return pol, pod, selected_voyage, sources


# =============================================================================
# 3. Main enrichment function
# =============================================================================

def enrich(
    db,
    memory_db,
    message_id: str,
    intake: IntakeExtraction,
    message_date: date,
    llm_calls: int,
    latency_ms: int,
    model: str,
) -> BookingIntakeResult:

    # --- 0. "other"-classified reconciliation: if this message reveals a
    # customer_reference <-> carrier_booking_no mapping (e.g. M-003's subject
    # line), backfill our memory of the original booking_request BEFORE doing
    # anything else — this is the message's real purpose, not a side effect. ---
    if (intake.classification == "other" and intake.carrier_booking_no
            and intake.customer_reference):
        backfill_carrier_booking_no(memory_db, intake.customer_reference, intake.carrier_booking_no)

    # --- 1. Gazetteer ---
    pol, pod, gazetteer_sources = resolve_routing_codes(db, intake)
    sources: dict[str, SourceEntry] = dict(gazetteer_sources)

    # --- 1b. bl_instruction: if the message itself didn't restate a route
    # (the normal case — it's confirming an existing booking), resolve
    # routing/voyage from what's already known about carrier_booking_no
    # instead of trying to derive it from nothing. ---
    used_bl_shortcut = False
    bl_selected_voyage = None
    if intake.classification == "bl_instruction" and not pol.code and not pod.code:
        bl_routing = resolve_bl_instruction_routing(db, memory_db, intake)
        if bl_routing:
            pol, pod, bl_selected_voyage, bl_sources = bl_routing
            sources.update(bl_sources)
            used_bl_shortcut = True

    # --- 2. Precedent ---
    booker_name = intake.booker.name if intake.booker else ""
    shipper_name = intake.shipper.name if intake.shipper else ""

    # Deterministic fallback: some clients ARE both booker and shipper (per
    # the relation table's own 'role' field, e.g. Orchard Produce, Ferro
    # Meteren). If the message doesn't restate a separate shipper (M-002:
    # no shipper label anywhere, cargo owner only identifiable via the
    # sender/signature), default shipper = booker — sourced from MASTER_DATA
    # (the relation table's role), not an LLM guess. Only applies when the
    # relation is KNOWN to double as shipper; unknown companies still come
    # back with an empty shipper rather than a silent assumption.
    relation_lookup = build_relation_lookup(db)
    booker_relation = resolve_relation(relation_lookup, booker_name) if booker_name else None
    shipper_defaulted_from_booker = False

    effective_shipper = intake.shipper  # what actually goes into parties.shipper
    if not shipper_name and booker_relation:
        role = booker_relation['record'].get('role', '') or ''
        if 'shipper' in role.lower():
            shipper_name = booker_relation['canonical']
            effective_shipper = intake.booker  # reuse the booker's extracted Party details
            shipper_defaulted_from_booker = True

    # goods_hint is only meaningful when there's exactly one cargo line —
    # match_precedent compares it against a booking's single 'goods' field,
    # so joining multiple lines' descriptions would never match anything
    # real and would just add noise to the goods tie-break. With >1 line
    # (e.g. M-005), skip the hint entirely and let route + recency decide.
    goods_hint = intake.cargo[0].goods_description if len(intake.cargo) == 1 else None
    precedent = match_precedent(
        db, booker_name, shipper_name,
        # Use the RESOLVED pol/pod, not intake.port_of_loading_raw directly —
        # for bl_instruction messages, step 1b above may have already
        # resolved these from memory/static fallback even though the
        # message itself restated no route. For every other message type
        # this is a no-op: pol.raw/pod.raw already equal
        # intake.port_of_loading_raw/port_of_discharge_raw at this point.
        pol.raw, pod.raw,
        goods=goods_hint,
        message_date=message_date,  # CRITICAL: without this, a same-day booking
        # (which could be this very message's own resulting record) can be
        # picked as its own "precedent" — see precedentMatcher.py's fix notes.
    )

    # --- 3. Field precedence: MESSAGE wins, else PRECEDENT, else null ---
    commodity_code = None
    payable = None
    quotation_ref = None
    precedent_booking_no = None
    precedent_reason = None

    # match_precedent ALWAYS returns a dict — "nothing found" is signaled by
    # booking_no == "NA", not by None. `if precedent:` would be wrong here,
    # since a non-empty dict is always truthy.
    precedent_found = precedent["booking_no"] != "NA"

    if precedent_found:
        precedent_booking_no = precedent["booking_no"]
        precedent_reason = precedent["reason"]

        booking_record = _lookup_booking(db, precedent["booking_no"])
        if booking_record is None:
            # matcher returned a booking_no that isn't actually in the table —
            # treat as no usable precedent rather than crashing on None access.
            precedent_reason += " (let op: boekingdetails niet gevonden in db)"
        else:
            commodity_code = booking_record["commodity"]
            sources["commodity_code"] = SourceEntry(
                source=SourceType.PRECEDENT, confidence=precedent["confidence"],
                evidence=f"boeking {precedent['booking_no']}",
            )
            payable = booking_record["payable"]
            sources["payable"] = SourceEntry(
                source=SourceType.PRECEDENT, confidence=precedent["confidence"],
                evidence=f"boeking {precedent['booking_no']}",
            )
            quotation_ref = booking_record["quotation"]
            sources["quotation_ref"] = SourceEntry(
                source=SourceType.PRECEDENT, confidence=precedent["confidence"],
                evidence=f"boeking {precedent['booking_no']}",
            )
    else:
        # No precedent at all (e.g. a genuinely new client — Ferro Meteren in
        # the sample data). Make this visible rather than leaving silent
        # nulls with no explanation — validation should turn this into a
        # concrete needs_review issue downstream.
        precedent_reason = precedent["reason"]  # "Geen precedent gevonden"

        # Even with no booking history, the relation table may still know
        # this booker's default payable terms (e.g. Delmar -> PRP, Orchard
        # -> COL). Ferro Meteren's default_payable is null in the db, so
        # this correctly still resolves to nothing for that specific case —
        # it's not a blanket fallback, just one more known source to check
        # before giving up. Source is MASTER_DATA, not PRECEDENT, since it
        # didn't come from any specific booking. Reuses booker_relation
        # already resolved above (for the shipper fallback) rather than
        # re-querying the relation table a second time.
        if booker_relation and booker_relation['record'].get('default_payable'):
            payable = booker_relation['record']['default_payable']
            sources["payable"] = SourceEntry(
                source=SourceType.MASTER_DATA, confidence=1.0,
                evidence=f"standaard payable voor {booker_relation['canonical']}",
            )

    # --- 4. Cargo lines ---
    cargo_lines = build_cargo_lines(intake, fallback_commodity_code=commodity_code)
    if cargo_lines:
        first = cargo_lines[0]
        sources["gross_weight_kg"] = SourceEntry(
            source=SourceType.MESSAGE, confidence=0.9,
            evidence=intake.evidence.get("gross_weight_kg", str(first.gross_weight_kg)),
        )
        sources["packages"] = SourceEntry(
            source=SourceType.MESSAGE, confidence=0.9,
            evidence=intake.evidence.get("packages_count", f"{first.packages.count} {first.packages.type}"),
        )

    # --- 5. Voyage selection ---
    etd_hint = parse_etd_hint(intake.etd_hint_raw, message_date)
    required_t = total_weight_tonnes(cargo_lines) if cargo_lines else 0.0

    selected_voyage = bl_selected_voyage  # already resolved above for bl_instruction, if found
    if not used_bl_shortcut and selected_voyage is None and pol.raw and pod.raw:
        # match_voyage resolves pol/pod itself via the gazetteer, so pass the
        # RAW text here (e.g. "Gand"), not pol.code — the matcher's own
        # resolution handles variants; requiring a pre-resolved code here
        # would just duplicate that work and lose the raw evidence text.
        #
        # NOTE: deliberately gated on `not used_bl_shortcut` — if the
        # bl_instruction shortcut ran but couldn't determine a SPECIFIC
        # voyage (only the route), we do NOT fall through to "find the best
        # voyage available now": that would guess a CURRENT sailing, which
        # could easily differ from whatever was actually booked weeks
        # earlier (capacity/closing dates have moved on). Better to leave
        # selected_voyage null and let validation honestly flag
        # NO_VOYAGE_SELECTED than silently present a plausible-looking but
        # possibly wrong voyage.
        voyage_result = match_voyage(
            db, pol.raw, pod.raw, message_date, required_t,
            requested_service=intake.requested_service_or_vessel_raw,
            etd_hint=etd_hint,
        )
        if voyage_result["selected_voyage"]:
            voyage_record = _lookup_voyage(db, voyage_result["selected_voyage"])
            if voyage_record is not None:
                selected_voyage = SelectedVoyage(
                    voyage_code=voyage_record["voyage_code"], vessel=voyage_record["vessel"],
                    ets_pol=voyage_record["ets_pol"], eta_pod=voyage_record["eta_pod"],
                    selection_reason=voyage_result["reason"],
                    alternatives=voyage_result["alternatives"],
                )
                sources["selected_voyage"] = SourceEntry(
                    source=SourceType.DERIVED, confidence=voyage_result["confidence"],
                    evidence=voyage_result["reason"],
                )
            # if voyage_record is None, matcher returned a code not in the
            # table — leave selected_voyage unset, let validation flag it.

    # --- 6. Shipper source ---
    if shipper_defaulted_from_booker:
        sources["shipper"] = SourceEntry(
            source=SourceType.MASTER_DATA, confidence=1.0,
            evidence=f"geen aparte shipper vermeld; relatie '{booker_relation['canonical']}' is bekend als booker en shipper",
        )
    elif intake.shipper and intake.shipper.name:
        sources["shipper"] = SourceEntry(
            source=SourceType.MESSAGE, confidence=0.9,
            evidence=intake.evidence.get("shipper", intake.shipper.name),
        )

    # --- 7. Assemble ---
    def to_party(p) -> Party:
        if p is None:
            return Party()
        return Party(name=p.name, address_lines=p.address_lines, country=p.country)

    result = BookingIntakeResult(
        message=MessageInfo(
            message_id=message_id,
            classification=Classification(intake.classification),
            classification_confidence=intake.classification_confidence,
            language=Language(intake.language),
        ),
        references=References(
            carrier_booking_no=intake.carrier_booking_no,
            customer_reference=intake.customer_reference,
            precedent_booking_no=precedent_booking_no,
            precedent_reason=precedent_reason,
        ),
        routing=Routing(
            port_of_loading=pol,
            port_of_discharge=pod,
            requested_departure=RequestedDeparture(
                mode=DepartureMode(intake.requested_departure_mode or "next_available"),
                service_or_vessel_raw=intake.requested_service_or_vessel_raw,
                etd_hint=etd_hint.isoformat() if etd_hint else None,
            ),
            selected_voyage=selected_voyage,
        ),
        parties=Parties(
            shipper=to_party(effective_shipper),
            consignee=to_party(intake.consignee),
            notify=to_party(intake.notify) if intake.notify else None,
            booker=to_party(intake.booker),
        ),
        cargo=cargo_lines,
        documentation=Documentation(
            bl_type=BLType(intake.bl_type_raw) if intake.bl_type_raw else None,
            marks_and_numbers=intake.marks_and_numbers,
            shipment_number=intake.shipment_number,
        ),
        commercial=Commercial(
            payable=payable, quotation_ref=quotation_ref,
        ),
        sources=Sources(**sources),
        validation=Validation(status=ValidationStatus.needs_review, issues=[]),  # validation tier fills this in next
        run_metadata=RunMetadata(
            prompt_versions={"intake": "intake-v1"},
            model=model, llm_calls=llm_calls, latency_ms=latency_ms,
        ),
    )

    # --- 8. Remember what we derived, for later bl_instruction lookups ---
    # Only for booking_request: that's the message type that actually
    # determines routing/voyage from scratch, and it's the ONLY type with a
    # customer_reference known early enough to key on. carrier_booking_no
    # isn't known yet at this point (SEAFLOW hasn't assigned/confirmed it to
    # us) — it gets backfilled later, when an 'other'-classified message
    # (like M-003) reveals the mapping. Upserting even when validation
    # blocked the message is intentional: partial derived info (e.g. a
    # resolved route with no voyage) is still worth remembering.
    if intake.classification == "booking_request" and intake.customer_reference:
        upsert_processed_booking(
            memory_db,
            customer_reference=intake.customer_reference,
            message_id=message_id,
            record={
                "pol_raw": pol.raw, "pol_code": pol.code,
                "pod_raw": pod.raw, "pod_code": pod.code,
                "selected_voyage": selected_voyage.model_dump() if selected_voyage else None,
                "commodity_code": commodity_code, "payable": payable, "quotation_ref": quotation_ref,
                "shipper_name": effective_shipper.name if effective_shipper else "",
                "booker_name": booker_name,
            },
        )

    return result


# =============================================================================
# 4. Pipeline entry point — date extraction + intake + enrichment in one call
# =============================================================================

def process_email(db, memory_db, message_id: str, raw_email: str) -> BookingIntakeResult:
    """
    Single entry point for the whole intake pipeline. Not pure (calls the
    LLM) — this is the orchestration layer; enrich() stays independently
    testable underneath it.
    """
    message_date = extract_message_date(raw_email)
    if message_date is None:
        # A missing/malformed Datum: header silently corrupts precedent
        # recency and voyage closing-date filtering downstream — hard-fail
        # rather than guessing (e.g. defaulting to date.today()).
        raise ValueError(
            f"[{message_id}] could not extract message date from 'Datum:' header — "
            "refusing to guess, since this feeds precedent and voyage matching."
        )

    from agents.intake_agent import run_intake_agent  # local import: keeps LLM
    # dependency optional for callers who only need enrich() in isolation
    import time

    llm_start = time.perf_counter()
    intake_outcome = run_intake_agent(raw_email)
    llm_latency_ms = int((time.perf_counter() - llm_start) * 1000)

    if intake_outcome["error"] is not None:
        raise RuntimeError(f"[{message_id}] intake extraction failed: {intake_outcome['error']}")

    return enrich(
        db, memory_db, message_id=message_id, intake=intake_outcome["parsed"], message_date=message_date,
        llm_calls=1, latency_ms=llm_latency_ms,
        model=intake_outcome["model"],
    )


if __name__ == "__main__":
    # --- parse_etd_hint sanity checks (no db/pydantic needed for these) ---
    assert parse_etd_hint("24/6", date(2026, 6, 17)) == date(2026, 6, 24)
    assert parse_etd_hint("24-6", date(2026, 6, 17)) == date(2026, 6, 24)
    assert parse_etd_hint("24/06/2026", date(2026, 6, 17)) == date(2026, 6, 24)
    # rollover: "1/1" requested from a December message should mean next year
    assert parse_etd_hint("1/1", date(2026, 12, 20)) == date(2027, 1, 1)
    assert parse_etd_hint(None, date(2026, 6, 17)) is None
    assert parse_etd_hint("nonsense", date(2026, 6, 17)) is None
    print("parse_etd_hint checks passed")

    # Smoke test with hand-built data mirroring M-001, bypassing the real
    # LLM call — useful for testing enrich() in isolation.
    intake_stub = IntakeExtraction(
        classification="booking_request",
        classification_confidence=0.96,
        language="nl",
        customer_reference="DF-026-00604",
        port_of_loading_raw="Gent",
        port_of_discharge_raw="Georgetown",
        requested_departure_mode="next_available",
        shipper={"name": "Kalico Minerals", "address_lines": [], "country": "United Kingdom"},
        booker={"name": "Delmar Forwarding", "address_lines": [], "country": "Netherlands"},
        cargo=[{
            "line_no": 1, "packages_count": 160, "packages_type": "Pallets",
            "goods_description": "BARIFLOW HD", "gross_weight_kg": 80432.0,
            "hs_code_raw": "2511.1000",
        }],
        evidence={"gross_weight_kg": "160 Pallets - 80.432 kg."},
    )

    from tinydb import TinyDB
    from pathlib import Path
    import tempfile
    # This file lives at App/enrichment/enrichment.py — Db/db.json is two
    # levels up (App/enrichment/ -> App/ -> project root -> Db/).
    db_path = Path(__file__).resolve().parent.parent.parent / "Db" / "db.json"
    db = TinyDB(str(db_path))

    # Use a throwaway memory store for this smoke test, not the real
    # outputs/agent_memory.json — keeps repeated test runs from
    # accumulating stale state across executions.
    _tmpdir = tempfile.TemporaryDirectory()
    memory_db = TinyDB(Path(_tmpdir.name) / "agent_memory.json")

    result = enrich(
        db, memory_db, message_id="M-001", intake=intake_stub, message_date=date(2026, 6, 17),
        llm_calls=1, latency_ms=1200, model="anthropic/claude-sonnet-4-5",
    )
    print(result.model_dump_json(indent=2))

    # --- Ferro Meteren stub: exercises the "no precedent found" path ---
    intake_stub_no_precedent = intake_stub.model_copy(update={
        "booker": {"name": "Ferro Meteren", "address_lines": [], "country": "Netherlands"},
        "shipper": {"name": "Ferro Meteren", "address_lines": [], "country": "Netherlands"},
    })
    result_no_precedent = enrich(
        db, memory_db, message_id="M-005", intake=intake_stub_no_precedent, message_date=date(2026, 6, 20),
        llm_calls=1, latency_ms=1200, model="anthropic/claude-sonnet-4-5",
    )
    assert result_no_precedent.references.precedent_booking_no is None
    assert result_no_precedent.references.precedent_reason == "Geen precedent gevonden"
    # Ferro Meteren's default_payable is null in the relation table too —
    # confirms the fallback correctly returns nothing rather than guessing.
    assert result_no_precedent.commercial.payable is None
    print("no-precedent case OK:", result_no_precedent.references.model_dump())

    # --- M-003/M-004 style reconciliation flow ---
    # 1. A fresh booking_request (not M-001, to avoid colliding with the
    #    real precedent 4468731 already in Db/db.json) gets processed and
    #    should be remembered in memory, keyed by customer_reference.
    intake_new_booking = intake_stub.model_copy(update={"customer_reference": "TEST-REF-001"})
    result_new = enrich(
        db, memory_db, message_id="M-TEST-1", intake=intake_new_booking, message_date=date(2026, 6, 17),
        llm_calls=1, latency_ms=1200, model="anthropic/claude-sonnet-4-5",
    )
    from memory.agent_memory import lookup_by_customer_reference, lookup_by_carrier_booking_no
    remembered = lookup_by_customer_reference(memory_db, "TEST-REF-001")
    assert remembered is not None, "booking_request should have been written to memory"
    assert remembered["pol_code"] == "BEGNE"
    print("memory upsert after booking_request OK:", remembered["pol_code"], remembered["pod_code"])

    # 2. An 'other'-classified reconciliation message reveals the mapping.
    intake_reconciliation = IntakeExtraction(
        classification="other",
        classification_confidence=0.95,
        language="nl",
        carrier_booking_no="TEST-BOOKING-999",
        customer_reference="TEST-REF-001",
        cargo=[],
    )
    enrich(
        db, memory_db, message_id="M-TEST-3", intake=intake_reconciliation, message_date=date(2026, 6, 19),
        llm_calls=1, latency_ms=1200, model="anthropic/claude-sonnet-4-5",
    )
    backfilled = lookup_by_carrier_booking_no(memory_db, "TEST-BOOKING-999")
    assert backfilled is not None, "carrier_booking_no should now be resolvable after the 'other' message"
    assert backfilled["customer_reference"] == "TEST-REF-001"
    print("reconciliation backfill OK:", backfilled["carrier_booking_no"])

    # 3. A bl_instruction referencing that carrier_booking_no should now
    #    resolve routing/voyage from memory, WITHOUT restating a route.
    intake_bl = IntakeExtraction(
        classification="bl_instruction",
        classification_confidence=1.0,
        language="nl",
        carrier_booking_no="TEST-BOOKING-999",
        customer_reference="TEST-REF-001",
        # deliberately no port_of_loading_raw/port_of_discharge_raw — a real
        # bl_instruction doesn't restate the route
        shipper={"name": "Kalico Minerals", "address_lines": [], "country": "United Kingdom"},
        cargo=[{
            "line_no": 1, "packages_count": 158, "packages_type": "pallets",
            "goods_description": "BARIFLOW HD", "gross_weight_kg": 79426.6,
        }],
    )
    result_bl = enrich(
        db, memory_db, message_id="M-TEST-4", intake=intake_bl, message_date=date(2026, 6, 19),
        llm_calls=1, latency_ms=1200, model="anthropic/claude-sonnet-4-5",
    )
    assert result_bl.routing.port_of_loading.code == "BEGNE", (
        "bl_instruction should resolve POL from memory, not come back empty", result_bl.routing
    )
    assert result_bl.routing.selected_voyage is not None
    assert result_bl.sources.selected_voyage.source == "SYSTEM"
    print("bl_instruction routing-from-memory OK:", result_bl.routing.port_of_loading.code,
          result_bl.routing.selected_voyage.voyage_code)

    # 4. Precedent-matching fix: a bl_instruction referencing a REAL
    #    historical booking (4471902, static Db fixture) should now get a
    #    STRONGER precedent match than before — previously match_precedent
    #    received the message's own EMPTY port fields (bl_instruction doesn't
    #    restate the route), landing on "andere route" (confidence 0.30).
    #    Now it receives the bl_instruction-resolved route, so a booking on
    #    the same actual route should match as "en route", not "andere route".
    # Fresh memory_db (no M-001 processed) so this specifically exercises the
    # static booking-table fallback path, not the memory path from test 3.
    fresh_memory_db = TinyDB(Path(_tmpdir.name) / "agent_memory_fresh.json")
    intake_bl_real = IntakeExtraction(
        classification="bl_instruction", classification_confidence=1.0, language="nl",
        carrier_booking_no="4471902",  # real Db/db.json fixture
        customer_reference="DF-026-00604",
        shipper={"name": "Kalico Minerals and Services Limited", "address_lines": [], "country": "United Kingdom"},
        booker={"name": "Delmar Forwarding B.V.", "address_lines": [], "country": "Netherlands"},
        cargo=[{
            "line_no": 1, "packages_count": 158, "packages_type": "pallets",
            "goods_description": "BARIFLOW HD", "gross_weight_kg": 79426.6,
        }],
    )
    result_bl_real = enrich(
        db, fresh_memory_db, message_id="M-TEST-4-real", intake=intake_bl_real,
        message_date=date(2026, 6, 19), llm_calls=1, latency_ms=1200,
        model="anthropic/claude-sonnet-4-5",
    )
    assert result_bl_real.routing.port_of_loading.code == "BEGNE"
    assert result_bl_real.references.precedent_booking_no is not None
    assert "andere route" not in (result_bl_real.references.precedent_reason or ""), (
        "precedent match should now see the resolved route as a MATCH, not a mismatch",
        result_bl_real.references.precedent_reason,
    )
    print("bl_instruction precedent-matching fix OK:", result_bl_real.references.precedent_reason)

    _tmpdir.cleanup()

    # Full end-to-end test (requires OPENROUTER_API_KEY and a real email):
    # raw_email = "...\nDatum: 17 juni 2026\n..."
    # result = process_email(db, memory_db, "M-001", raw_email)
    # print(result.model_dump_json(indent=2))