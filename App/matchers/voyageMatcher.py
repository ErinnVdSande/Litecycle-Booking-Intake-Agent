import tinydb
import datetime

try:
    # Standalone execution from inside App/matchers/ — gazetteer.py resolves
    # flat since it's this file's own sibling.
    from gazetteer import build_port_lookup, build_service_lookup, resolve_port_fuzzy, resolve_service_fuzzy
except ImportError:
    # Imported as part of the whole app via main.py (App/ on sys.path).
    from matchers.gazetteer import (
        build_port_lookup, build_service_lookup, resolve_port_fuzzy, resolve_service_fuzzy,
    )

# laadhaven, loshaven en gevraagde vertrekdatum met een blik op de resterende capaciteit
#
# History:
#   - original bug: date.fromisoformat() called with only `datetime` imported
#     (NameError whenever etd_hint was actually used); message_date type-hinted
#     as datetime.date but the body assumed a string. Fixed via _to_date(),
#     which accepts either.
#   - first fuzzy attempt: raw token_set_ratio string comparison on pol/pod/
#     service. WRONG — let "CX" silently score 100 against "CX Service"
#     (subset match, not actually fuzzy), and failed legitimate variants like
#     "Gand" vs "Gent" (~50 similarity, nowhere near a usable threshold).
#   - second approach: resolved pol/pod/service to CODES via a gazetteer
#     module with its own hardcoded copy of the alias data.
#   - CURRENT approach: the gazetteer reads port/service alias data directly
#     from the db (port/service tables) instead of a hardcoded duplicate that
#     could drift out of sync. Matching uses the canonical NAME ("Gent"), not
#     the code ("BEGNE") — because that's what booking/voyage actually store,
#     so no extra resolution layer is needed for the db-side records.


def _to_date(d) -> datetime.date:
    """Accepts a datetime.date OR an ISO 'YYYY-MM-DD' string; always returns a date."""
    if isinstance(d, datetime.date):
        return d
    return datetime.datetime.strptime(d, "%Y-%m-%d").date()


def match_voyage(
    db,
    pol: str,
    pod: str,
    message_date: datetime.date | str,
    required_capacity_t: float,
    requested_service: str | None = None,
    etd_hint: datetime.date | str | None = None,
) -> dict:
    message_date = _to_date(message_date)
    if etd_hint is not None:
        etd_hint = _to_date(etd_hint)

    port_lookup = build_port_lookup(db)
    service_lookup = build_service_lookup(db)

    # --- 1. Route filter — via gazetteer-resolved canonical names ---
    pol_match = resolve_port_fuzzy(port_lookup, pol)
    pod_match = resolve_port_fuzzy(port_lookup, pod)
    pol_name = pol_match.name if pol_match else pol.strip().lower()
    pod_name = pod_match.name if pod_match else pod.strip().lower()
    pol_conf = pol_match.confidence if pol_match else 0.0
    pod_conf = pod_match.confidence if pod_match else 0.0

    all_voyages = db.table("voyage").all()
    scored = []
    for v in all_voyages:
        # voyage.pol/pod are already canonical names in this dataset, but
        # resolve them too rather than assume — cheap, and defends against
        # a future voyage row using a variant spelling by mistake.
        v_pol_match = resolve_port_fuzzy(port_lookup, v['pol'])
        v_pod_match = resolve_port_fuzzy(port_lookup, v['pod'])
        v_pol_name = v_pol_match.name if v_pol_match else v['pol'].strip().lower()
        v_pod_name = v_pod_match.name if v_pod_match else v['pod'].strip().lower()
        if v_pol_name == pol_name and v_pod_name == pod_name:
            scored.append({'record': v})

    if not scored:
        return {
            'selected_voyage': None,
            'alternatives': [],
            'reason': 'geen dienst gevonden op deze route',
            'confidence': 0.0,
        }

    # --- 2. Hard filter: still bookable (closing date not passed) ---
    bookable = [s for s in scored if _to_date(s['record']['closing_pol']) >= message_date]
    if not bookable:
        return {
            'selected_voyage': None,
            'alternatives': [],
            'reason': 'geen afvaart meer open voor boeking op deze route',
            'confidence': 0.0,
        }

    # --- 3. Hard filter: enough remaining capacity ---
    def remaining(v):
        return v['capacity_t'] - v['booked_t']

    with_capacity = [s for s in bookable if remaining(s['record']) >= required_capacity_t]
    if not with_capacity:
        return {
            'selected_voyage': None,
            'alternatives': [],
            'reason': 'geen afvaart met voldoende capaciteit op deze route',
            'confidence': 0.0,
        }

    # --- 4. Soft filter / flag: requested service — via gazetteer, doesn't hard-gate ---
    service_note = None
    service_conf = 1.0  # no penalty if no service was requested at all
    if requested_service:
        req_service_match = resolve_service_fuzzy(service_lookup, requested_service)
        req_service_code = req_service_match.code if req_service_match else None
        req_service_conf = req_service_match.confidence if req_service_match else 0.0

        service_matched = []
        for s in with_capacity:
            v_service_match = resolve_service_fuzzy(service_lookup, s['record']['service'])
            v_service_code = v_service_match.code if v_service_match else None
            if req_service_code is not None and v_service_code == req_service_code:
                service_matched.append(s)

        if service_matched:
            with_capacity = service_matched
            service_conf = req_service_conf  # 1.0 if exact synonym, <1.0 if fuzzy fallback
        else:
            best_alt = with_capacity[0]['record']['service']
            service_note = (
                f"gevraagde dienst '{requested_service}' komt niet voor op deze "
                f"route; dienst {best_alt} gebruikt"
            )
            service_conf = 0.6

    # --- 5. Rank by departure date ---
    if etd_hint:
        ranked = sorted(with_capacity, key=lambda s: abs((_to_date(s['record']['ets_pol']) - etd_hint).days))
        date_reason = f"dichtst bij gevraagde datum {etd_hint.isoformat()} met voldoende capaciteit"
    else:
        ranked = sorted(with_capacity, key=lambda s: s['record']['ets_pol'])
        date_reason = "eerste afvaart na boekingsdatum met voldoende capaciteit"

    best = ranked[0]
    rest = ranked[1:]

    # --- 6. Confidence: base * route-resolution-confidence * service-confidence ---
    base_confidence = 0.85
    route_confidence = min(pol_conf, pod_conf)
    confidence = round(base_confidence * route_confidence * service_conf, 2)

    fuzzy_notes = []
    if route_confidence < 1.0:
        fuzzy_notes.append(
            f"route via gazetteer benaderend opgelost (pol '{pol}' -> '{pol_name}', "
            f"pod '{pod}' -> '{pod_name}')"
        )
    if service_note:
        fuzzy_notes.append(service_note)
    elif service_conf < 1.0:
        fuzzy_notes.append(f"dienst via gazetteer benaderend opgelost ('{requested_service}')")

    reason = date_reason
    if fuzzy_notes:
        reason += "; let op: " + "; ".join(fuzzy_notes)

    return {
        'selected_voyage': best['record']['voyage_code'],
        'alternatives': [s['record']['voyage_code'] for s in rest[:2]],
        'reason': reason,
        'confidence': confidence,
    }


if __name__ == "__main__":
    from pathlib import Path
    # This file lives at App/matchers/voyageMatcher.py — Db/db.json is two
    # levels up (App/matchers/ -> App/ -> project root -> Db/).
    db_path = Path(__file__).resolve().parent.parent.parent / "Db" / "db.json"
    db = tinydb.TinyDB(str(db_path))

    # exact match, string date
    exact = match_voyage(db, "Gent", "Georgetown", "2026-06-10", 100)
    print("exact (string date):", exact)
    assert exact["selected_voyage"] == "CX2614", exact
    assert exact["confidence"] == 0.85, exact  # no penalty: both ports resolve exactly

    # exact match, real date.date object
    exact_dateobj = match_voyage(db, "Gent", "Georgetown", datetime.date(2026, 6, 10), 100)
    print("exact (date object): ", exact_dateobj)
    assert exact_dateobj["selected_voyage"] == "CX2614"
    assert exact_dateobj["confidence"] == exact["confidence"]

    # "Gand" is a listed alias for Gent — exact alias lookup via the db,
    # should resolve at FULL confidence, not be flagged as fuzzy.
    alias_port = match_voyage(db, "Gand", "Georgetown", "2026-06-10", 100)
    print("alias port (Gand):", alias_port)
    assert alias_port["selected_voyage"] == "CX2614", alias_port
    assert alias_port["confidence"] == exact["confidence"], "a listed alias is an EXACT match, not fuzzy"

    # exact service alias — "CX" is itself a listed synonym for the CX service
    exact_service = match_voyage(db, "Gent", "Georgetown", "2026-06-10", 100, requested_service="CX")
    print("service alias (CX):", exact_service)
    assert "benaderend" not in exact_service["reason"]
    assert exact_service["confidence"] == exact["confidence"]

    # genuine typo not in the alias table — should hit the gazetteer's fuzzy
    # fallback and come back at reduced confidence
    typo_port = match_voyage(db, "Gnet", "Georgetown", "2026-06-10", 100)  # transposed letters
    print("typo port (Gnet):", typo_port)

    # etd_hint as a date object
    with_etd = match_voyage(db, "Gent", "Paramaribo", "2026-06-01", 30, etd_hint=datetime.date(2026, 7, 8))
    print("with etd_hint:", with_etd)

    print("checks passed")
