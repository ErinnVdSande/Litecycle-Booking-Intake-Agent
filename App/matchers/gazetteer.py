"""
Gazetteer tier — resolves free-text mentions against closed master-data
vocabularies (§6.3), read directly from the db (port/service tables) rather
than a hardcoded duplicate. Two sources of the same alias data drift out of
sync; the db.json port/service tables are the single source of truth.

Usage pattern:
    port_lookup = build_port_lookup(db)      # once per matcher call
    match = resolve_port(port_lookup, "Gand")  # many times, cheap, pure function

Splitting "load from db" (I/O) from "resolve one value" (pure dict lookup)
means resolve_port/resolve_service are trivially unit-testable with a
hand-built dict — no tinydb needed for that part at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GazetteerMatch:
    code: str
    name: str | None   # canonical name as stored in booking/voyage (e.g. "Gent")
    confidence: float
    evidence: str


def _normalize(text: str) -> str:
    return text.strip().lower()


# ---------------------------------------------------------------------------
# Lookup builders — read the db ONCE per matcher call, not once per resolve
# ---------------------------------------------------------------------------

def build_port_lookup(db) -> dict[str, dict]:
    """normalized alias/name -> {'name': canonical name, 'code': UN/LOCODE-style code}"""
    lookup = {}
    for rec in db.table('port').all():
        candidates = [rec['name']] + list(rec.get('variants', []))
        for candidate in candidates:
            lookup[_normalize(candidate)] = {'name': rec['name'], 'code': rec['code']}
    return lookup


def build_service_lookup(db) -> dict[str, dict]:
    """normalized synonym -> {'name': None (service table has no display name), 'code': service code}"""
    lookup = {}
    for rec in db.table('service').all():
        for synonym in rec.get('synonyms', []):
            lookup[_normalize(synonym)] = {'name': None, 'code': rec['code']}
    return lookup


def build_relation_lookup(db) -> dict[str, dict]:
    """
    normalized name/synonym -> {'canonical': short name as stored in booking/
    voyage history, 'record': full relation record (debtor, default_payable, role)}

    The relation table's 'name' field is the full legal name ("Delmar
    Forwarding B.V.", closer to what an LLM extracts from an email
    signature); 'synonyms' is the short form actually used as booker/shipper
    in the booking table ("Delmar Forwarding"). The canonical match key is
    the SHORT form, since that's what booking history compares against —
    resolving to the long form would just reintroduce the mismatch this is
    meant to fix.

    Note: db.json currently stores 'synonyms' as a single string, not a
    list (unlike port/service) — handled below, but worth normalizing to a
    list in the db if more than one synonym per relation is ever needed.
    """
    lookup = {}
    for rec in db.table('relation').all():
        raw_synonyms = rec.get('synonyms')
        if isinstance(raw_synonyms, str) and raw_synonyms:
            synonyms = [raw_synonyms]
        elif isinstance(raw_synonyms, list):
            synonyms = raw_synonyms
        else:
            synonyms = []

        canonical = synonyms[0] if synonyms else rec['name']
        candidates = [rec['name'], *synonyms]
        for candidate in candidates:
            lookup[_normalize(candidate)] = {'canonical': canonical, 'record': rec}
    return lookup


def resolve_relation(relation_lookup: dict, raw: str) -> dict | None:
    """Returns {'canonical': str, 'record': dict} or None if unresolvable.
    Exact lookup only — no fuzzy fallback here, since an unresolved relation
    should fall through to the precedent matcher's own fuzzy company-name
    matching (open-vocabulary text), not silently guess a relation."""
    return relation_lookup.get(_normalize(raw))


# ---------------------------------------------------------------------------
# Exact-match resolvers — pure functions over an already-built lookup
# ---------------------------------------------------------------------------

def resolve_port(port_lookup: dict, raw: str) -> GazetteerMatch | None:
    entry = port_lookup.get(_normalize(raw))
    if entry is None:
        return None
    return GazetteerMatch(
        code=entry['code'], name=entry['name'], confidence=1.0,
        evidence=f"{raw} -> {entry['name']} ({entry['code']})",
    )


def resolve_service(service_lookup: dict, raw: str) -> GazetteerMatch | None:
    entry = service_lookup.get(_normalize(raw))
    if entry is None:
        return None
    return GazetteerMatch(
        code=entry['code'], name=entry['name'], confidence=1.0,
        evidence=f"{raw} -> {entry['code']}",
    )


# bl_type has no db table (it's a fixed two-value enum, not master data with
# variants/synonyms) — kept as a small static lookup, not db-backed.
_BL_TYPE_LOOKUP = {"express": "express", "original": "original"}


def resolve_bl_type(raw: str) -> GazetteerMatch | None:
    code = _BL_TYPE_LOOKUP.get(_normalize(raw))
    if code is None:
        return None
    return GazetteerMatch(code=code, name=None, confidence=1.0, evidence=f"{raw} -> {code}")


# ---------------------------------------------------------------------------
# Fuzzy fallback — only reached if exact lookup fails
# Requires: pip install rapidfuzz
# ---------------------------------------------------------------------------

def resolve_port_fuzzy(port_lookup: dict, raw: str, min_score: float = 85.0) -> GazetteerMatch | None:
    exact = resolve_port(port_lookup, raw)
    if exact is not None:
        return exact

    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return None  # fuzzy matching unavailable, fail closed rather than guess

    result = process.extractOne(_normalize(raw), port_lookup.keys(), scorer=fuzz.ratio)
    if result is None:
        return None
    match, score, _ = result
    if score < min_score:
        return None
    entry = port_lookup[match]
    confidence = round(0.5 + (score / 100.0) * 0.4, 2)  # caps out around 0.9
    return GazetteerMatch(
        code=entry['code'], name=entry['name'], confidence=confidence,
        evidence=f"{raw} ~ {match} -> {entry['name']} ({entry['code']})",
    )


def resolve_service_fuzzy(service_lookup: dict, raw: str, min_score: float = 85.0) -> GazetteerMatch | None:
    exact = resolve_service(service_lookup, raw)
    if exact is not None:
        return exact

    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return None

    result = process.extractOne(_normalize(raw), service_lookup.keys(), scorer=fuzz.ratio)
    if result is None:
        return None
    match, score, _ = result
    if score < min_score:
        return None
    entry = service_lookup[match]
    confidence = round(0.5 + (score / 100.0) * 0.4, 2)
    return GazetteerMatch(
        code=entry['code'], name=entry['name'], confidence=confidence,
        evidence=f"{raw} ~ {match} -> {entry['code']}",
    )


if __name__ == "__main__":
    import tinydb
    from pathlib import Path
    # This file lives at App/matchers/gazetteer.py — Db/db.json is two
    # levels up (App/matchers/ -> App/ -> project root -> Db/).
    db_path = Path(__file__).resolve().parent.parent.parent / "Db" / "db.json"
    db = tinydb.TinyDB(str(db_path))

    port_lookup = build_port_lookup(db)
    service_lookup = build_service_lookup(db)

    # exact alias — from the db, not a hardcoded copy
    m = resolve_port(port_lookup, "Gand")
    assert m is not None and m.name == "Gent" and m.code == "BEGNE", m
    print("Gand ->", m)

    m = resolve_port(port_lookup, "Ghent")
    assert m.name == "Gent"

    m = resolve_port(port_lookup, "Antwerp")
    assert m.name == "Antwerpen" and m.code == "BEANR"

    assert resolve_port(port_lookup, "Nowhereville") is None

    # service — "CX" IS a listed synonym, exact match, not fuzzy
    m = resolve_service(service_lookup, "CX")
    assert m is not None and m.code == "CX"
    print("CX ->", m)

    m = resolve_service(service_lookup, "Continent Express")
    assert m.code == "CX"

    # relation synonyms — the actual case that motivated this fix
    relation_lookup = build_relation_lookup(db)
    m = resolve_relation(relation_lookup, "Delmar Forwarding B.V.")
    assert m is not None and m['canonical'] == "Delmar Forwarding", m
    print("Delmar Forwarding B.V. ->", m)

    m = resolve_relation(relation_lookup, "Delmar Forwarding")  # already-short form
    assert m is not None and m['canonical'] == "Delmar Forwarding"

    assert resolve_relation(relation_lookup, "Kalico Minerals") is None  # not in relation table at all

    print("all checks passed")
