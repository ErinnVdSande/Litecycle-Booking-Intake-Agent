import tinydb
import datetime
from rapidfuzz import fuzz

try:
    # Standalone execution from inside App/matchers/ (e.g. `python
    # precedentMatcher.py`) — Python puts this file's own directory on
    # sys.path, so gazetteer.py (same folder) resolves flat.
    from gazetteer import build_port_lookup, resolve_port_fuzzy, build_relation_lookup, resolve_relation
except ImportError:
    # Imported as part of the whole app (e.g. via main.py, which puts App/
    # on sys.path) — gazetteer needs its matchers. prefix in that context.
    from matchers.gazetteer import (
        build_port_lookup, resolve_port_fuzzy, build_relation_lookup, resolve_relation,
    )

# 1.filter on booker and shipper (fuzzy — company names vary: "Kalico Minerals"
#   vs "Kalico Minerals and Services Limited" are the same company)
# 2.generate score for results
# 3.rank
# 4.explain
# Momenteel is deze stap manuele regels maar deze kan worden omgezet naar een geleerde stap
# LightGBM, XGBoost ranker LambdaMart-style
#
# Route (pol/pod) comparison goes through the gazetteer (db-backed port
# table), not raw string equality — the booking table stores plain port
# names ("Gent"), which have known variants ("Gand", "Ghent"). Comparing raw
# text with == would miss a same-route match if the email and the booking
# history happen to use different variants of the same port.
#
# BUG FIXED: match_precedent had no temporal boundary — without one, it just
# returns the globally most-recent matching booking, with no notion of "as
# of when". Concretely: booking 4471902 in the sample data IS the record
# that results from processing M-001 (its copied_from points to 4468731,
# the actual correct precedent) and is dated the same day as M-001 itself.
# Without a cutoff, the matcher was returning 4471902 as M-001's own
# precedent — matching itself, in effect. `message_date` now excludes any
# booking dated on or after the message being processed.

NAME_MATCH_THRESHOLD = 80   # below this: not considered a candidate at all


def _is_exact(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _candidate_score(a: str, b: str) -> float:
    # Permissive filter metric: token_set_ratio scores a full subset match
    # ("Kalico Minerals" vs "Kalico Minerals and Services Limited") as 100,
    # by design — it's built to ignore extra tokens entirely. That makes it
    # great for deciding "is this plausibly the same company" but useless
    # for deciding "how different are these strings", since it returns 100
    # for lots of non-identical pairs. Used ONLY for the >=threshold filter.
    return fuzz.token_set_ratio(a.strip().lower(), b.strip().lower())


def _fuzziness_score(a: str, b: str) -> float:
    # Length-sensitive metric, used ONLY to size the confidence penalty once
    # we already know (via _is_exact) that the pair isn't identical.
    # token_sort_ratio still tolerates word-order differences but, unlike
    # token_set_ratio, it does NOT collapse to 100 just because one string
    # is a subset of the other — extra words genuinely lower the score.
    return fuzz.token_sort_ratio(a.strip().lower(), b.strip().lower())


def _port_name(port_lookup: dict, raw: str) -> str:
    match = resolve_port_fuzzy(port_lookup, raw)
    return match.name if match else raw.strip().lower()


def _relation_name(relation_lookup: dict, raw: str) -> str:
    """Resolve a raw booker/shipper name to the SHORT form used in booking
    history, via the relation table's known synonyms — e.g. "Delmar
    Forwarding B.V." (as an LLM would extract from a signature) resolves to
    "Delmar Forwarding" (as stored in booking.booker). Falls through to the
    raw text unchanged if the company isn't in the relation table at all
    (e.g. Kalico Minerals, which only ever appears as a shipper, never a
    booker relation) — in that case the existing fuzzy matching below still
    handles it."""
    match = resolve_relation(relation_lookup, raw)
    return match['canonical'] if match else raw


def _to_date_str(d) -> str:
    """Accepts a datetime.date OR an ISO 'YYYY-MM-DD' string; always returns
    the ISO string, for comparing against booking['date'] (stored as string)."""
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return d


def match_precedent(db, booker, shipper, pol, pod, goods=None, message_date=None) -> dict:
    """
    message_date: if given (datetime.date or ISO string), excludes any
    booking dated on or after this date — a precedent must be genuinely
    PRIOR history, not a same-day-or-later record (which could be the
    outcome of the very message being processed, as with 4471902/M-001).
    If omitted, no temporal cutoff is applied (returns the globally
    most-recent match) — fine for exploratory/ad-hoc queries, but
    production callers (enrichment.py) MUST pass message_date.
    """
    all_bookings = db.table('booking').all()
    port_lookup = build_port_lookup(db)
    relation_lookup = build_relation_lookup(db)

    cutoff = _to_date_str(message_date) if message_date is not None else None
    if cutoff is not None:
        all_bookings = [b for b in all_bookings if b['date'] < cutoff]

    # Resolve known relations to their canonical short form BEFORE fuzzy
    # matching — this is a curated alias table, same principle as ports and
    # services: known synonyms get exact lookup, fuzzy matching is only for
    # the genuinely unknown/open-vocabulary remainder.
    booker_resolved = _relation_name(relation_lookup, booker)
    shipper_resolved = _relation_name(relation_lookup, shipper)

    scored = []
    for b in all_bookings:
        booker_score = _candidate_score(booker_resolved, b['booker'])
        shipper_score = _candidate_score(shipper_resolved, b['shipper'])
        if booker_score >= NAME_MATCH_THRESHOLD and shipper_score >= NAME_MATCH_THRESHOLD:
            scored.append({
                'record': b,
                'booker_exact': _is_exact(booker_resolved, b['booker']),
                'shipper_exact': _is_exact(shipper_resolved, b['shipper']),
            })

    if not scored:
        return {'booking_no': 'NA', 'confidence': 0, 'reason': 'Geen precedent gevonden'}

    pol_name = _port_name(port_lookup, pol)
    pod_name = _port_name(port_lookup, pod)
    same_route = [
        s for s in scored
        if _port_name(port_lookup, s['record']['pol']) == pol_name
        and _port_name(port_lookup, s['record']['pod']) == pod_name
    ]
    pool, confidence = (same_route, 0.90) if same_route else (scored, 0.60)

    reason = f"match op booker, shipper{' en route' if same_route else ' (andere route)'}"

    if goods:
        goods_matched = [s for s in pool if s['record']['goods'] == goods]
        if goods_matched:
            pool = goods_matched
            reason += " en goods"
        else:
            confidence -= 0.05
            reason += " (andere goods)"

    best = max(pool, key=lambda s: s['record']['date'])

    # --- fuzzy-match penalty: score-dependent, not a flat deduction ---
    is_exact = best['booker_exact'] and best['shipper_exact']
    if not is_exact:
        fuzzy_fields = []
        penalties = []

        if not best['booker_exact']:
            score = _fuzziness_score(booker_resolved, best['record']['booker'])
            penalties.append(min(0.30, (100 - score) * 0.01))
            fuzzy_fields.append(f"booker '{booker_resolved}' ~ '{best['record']['booker']}'")

        if not best['shipper_exact']:
            score = _fuzziness_score(shipper_resolved, best['record']['shipper'])
            penalties.append(min(0.30, (100 - score) * 0.01))
            fuzzy_fields.append(f"shipper '{shipper_resolved}' ~ '{best['record']['shipper']}'")

        fuzzy_penalty = max(penalties)
        confidence = round(max(0.05, confidence - fuzzy_penalty), 2)
        reason += f"; benaderende naam-match ({', '.join(fuzzy_fields)})"

    booking_no = best['record']['booking_no']

    return {
        'booking_no': booking_no,
        'confidence': confidence,
        'reason': reason,
    }


if __name__ == "__main__":
    from pathlib import Path
    # This file lives at App/matchers/precedentMatcher.py — Db/db.json is
    # two levels up (App/matchers/ -> App/ -> project root -> Db/).
    db_path = Path(__file__).resolve().parent.parent.parent / "Db" / "db.json"
    db = tinydb.TinyDB(str(db_path))

    # sanity check on the metric mismatch that caused the earlier fuzzy bug —
    # token_set_ratio collapses to 100 on a subset match, token_sort_ratio doesn't
    print("token_set_ratio :", fuzz.token_set_ratio("kalico minerals", "kalico minerals and services limited"))
    print("token_sort_ratio:", fuzz.token_sort_ratio("kalico minerals", "kalico minerals and services limited"))

    # --- THE actual bug that motivated this fix: without message_date, the
    # matcher returns 4471902 — which is M-001's OWN resulting booking, not
    # a real precedent. With message_date=2026-06-17 (M-001's date), it must
    # correctly exclude 4471902 and return 4468731 instead. ---
    no_cutoff = match_precedent(db, "Delmar Forwarding", "Kalico Minerals", "Gent", "Georgetown")
    print("no message_date (buggy in production, fine for ad-hoc queries):", no_cutoff)
    assert no_cutoff["booking_no"] == "4471902"  # globally most recent — expected WITHOUT a cutoff

    correct = match_precedent(
        db, "Delmar Forwarding", "Kalico Minerals", "Gent", "Georgetown",
        message_date=datetime.date(2026, 6, 17),  # M-001's own message date
    )
    print("with message_date=2026-06-17 (correct for production):", correct)
    assert correct["booking_no"] == "4468731", (
        "message_date should exclude 4471902 (dated the same day, and it's "
        "actually M-001's own outcome) and fall back to the real precedent", correct
    )
    assert correct["confidence"] == 0.90, correct
    assert "benaderende" not in correct["reason"]

    # route via a port ALIAS — "Gand" is a listed variant of Gent (§6.3, db-backed).
    # Must still count as "same route" (0.90), not fall through to "andere route" (0.60).
    alias_route = match_precedent(
        db, "Delmar Forwarding", "Kalico Minerals", "Gand", "Georgetown",
        message_date=datetime.date(2026, 6, 17),
    )
    print(alias_route)
    assert alias_route["booking_no"] == "4468731"
    assert alias_route["confidence"] == 0.90, ("a port alias should still count as the same route", alias_route)
    assert "en route" in alias_route["reason"]

    # booker via a RELATION synonym — "Delmar Forwarding B.V." is the full
    # legal name in the relation table; booking history stores the short
    # form "Delmar Forwarding". This should now resolve EXACTLY via the
    # relation table, not fall through to fuzzy string matching.
    relation_alias = match_precedent(
        db, "Delmar Forwarding B.V.", "Kalico Minerals", "Gent", "Georgetown",
        message_date=datetime.date(2026, 6, 17),
    )
    print(relation_alias)
    assert relation_alias["booking_no"] == "4468731"
    assert relation_alias["confidence"] == 0.90, (
        "a relation synonym should resolve exactly, not fall back to fuzzy", relation_alias
    )
    assert "benaderende" not in relation_alias["reason"]

    # fuzzy match — company name variant NOT covered by the relation table
    # (Kalico Minerals only ever appears as a shipper, never a booker
    # relation) — should still fall through to fuzzy matching as before.
    fuzzy = match_precedent(
        db, "Delmar Forwarding", "Kalico Minerals and Services Limited", "Gent", "Georgetown",
        message_date=datetime.date(2026, 6, 17),
    )
    print(fuzzy)
    assert fuzzy["booking_no"] == "4468731"
    assert fuzzy["confidence"] < 0.90, "fuzzy match should score lower than an exact one"
    assert "benaderende naam-match" in fuzzy["reason"]

    # negative case — should NOT match a different Kalico-prefixed entity
    consignee_name = "Kalico Guyana Inc."
    no_match = match_precedent(
        db, "Delmar Forwarding", consignee_name, "Gent", "Georgetown",
        message_date=datetime.date(2026, 6, 17),
    )
    print(no_match)

    print("checks passed")
