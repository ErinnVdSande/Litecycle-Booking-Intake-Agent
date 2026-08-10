from pydantic import BaseModel, Field
from enum import Enum
from typing import Literal

# ---------------------------------------------------------------------------
# Enums / allow-lists (§7: "Zorg dat een model daar niet buiten kan vallen")
# ---------------------------------------------------------------------------

class Classification(str, Enum):
    booking_request = "booking_request"
    bl_instruction = "bl_instruction"
    other = "other"


class Language(str, Enum):
    nl = "nl"
    en = "en"
    mixed = "mixed"


class DepartureMode(str, Enum):
    next_available = "next_available"
    specified = "specified"


class WeightBasis(str, Enum):
    stated = "stated"
    derived = "derived"
    unknown = "unknown"


class BLType(str, Enum):
    express = "express"
    original = "original"


class Payable(str, Enum):
    PRP = "PRP"  # prepaid
    COL = "COL"  # collect


class SourceType(str, Enum):
    MESSAGE = "MESSAGE"
    PRECEDENT = "PRECEDENT"
    MASTER_DATA = "MASTER_DATA"
    DERIVED = "DERIVED"
    SYSTEM = "SYSTEM"


class ValidationStatus(str, Enum):
    ready = "ready"
    needs_review = "needs_review"
    blocked = "blocked"


class IssueSeverity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


# Extend as your master data grows (§6.3). Keep this the single source of
# truth for valid port/commodity codes so validation can check against it.
PortCode = Literal["BEGNE", "BEANR", "GYGEO", "SRPBM"]
CommodityCode = Literal["999", "141", "310", "401"]


# ---------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------

class MessageInfo(BaseModel):
    message_id: str
    classification: Classification
    classification_confidence: float = Field(ge=0.0, le=1.0)
    language: Language


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------

class References(BaseModel):
    carrier_booking_no: str | None = None  # filled on bl_instruction
    customer_reference: str | None = None
    precedent_booking_no: str | None = None
    precedent_reason: str | None = None


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

class PortRef(BaseModel):
    raw: str
    code: PortCode | None = None  # None if unresolved against master data


class RequestedDeparture(BaseModel):
    mode: DepartureMode
    service_or_vessel_raw: str | None = None
    etd_hint: str | None = None  # ISO date string if given


class SelectedVoyage(BaseModel):
    voyage_code: str
    vessel: str
    ets_pol: str  # ISO date
    eta_pod: str  # ISO date
    selection_reason: str
    alternatives: list[str] = Field(default_factory=list)


class Routing(BaseModel):
    port_of_loading: PortRef
    port_of_discharge: PortRef
    requested_departure: RequestedDeparture
    selected_voyage: SelectedVoyage | None = None  # None until enrichment runs


# ---------------------------------------------------------------------------
# parties
# ---------------------------------------------------------------------------

class Party(BaseModel):
    name: str = ""
    address_lines: list[str] = Field(default_factory=list)
    country: str = ""


class Parties(BaseModel):
    shipper: Party
    consignee: Party
    notify: Party | None = None
    booker: Party


# ---------------------------------------------------------------------------
# cargo
# ---------------------------------------------------------------------------

class Packages(BaseModel):
    count: int
    type: str


class InnerPackages(BaseModel):
    count: int
    unit: str
    description: str


class CargoLine(BaseModel):
    line_no: int
    packages: Packages
    inner_packages: InnerPackages | None = None
    goods_description: str
    gross_weight_kg: float
    weight_basis: WeightBasis
    hs_code: str | None = None  # normalized, digits only
    commodity_code: CommodityCode


# ---------------------------------------------------------------------------
# documentation
# ---------------------------------------------------------------------------

class Documentation(BaseModel):
    bl_type: BLType | None = None
    marks_and_numbers: str | None = None
    shipment_number: str | None = None


# ---------------------------------------------------------------------------
# commercial
# ---------------------------------------------------------------------------

class Commercial(BaseModel):
    payable: Payable | None = None
    quotation_ref: str | None = None


# ---------------------------------------------------------------------------
# sources — exactly these 8 fields per the assignment ("niet meer")
# ---------------------------------------------------------------------------

SOURCE_FIELDS = (
    "gross_weight_kg",
    "packages",
    "port_of_loading",
    "selected_voyage",
    "commodity_code",
    "payable",
    "quotation_ref",
    "shipper",
)


class SourceEntry(BaseModel):
    source: SourceType
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


class Sources(BaseModel):
    gross_weight_kg: SourceEntry | None = None
    packages: SourceEntry | None = None
    port_of_loading: SourceEntry | None = None
    selected_voyage: SourceEntry | None = None
    commodity_code: SourceEntry | None = None
    payable: SourceEntry | None = None
    quotation_ref: SourceEntry | None = None
    shipper: SourceEntry | None = None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

class ValidationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    field: str
    message: str
    suggested_action: str


class Validation(BaseModel):
    status: ValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# clarifications (optional communication agent output)
# ---------------------------------------------------------------------------

class Clarification(BaseModel):
    field: str
    question: str
    draft_message: str
    approved: bool = False


# ---------------------------------------------------------------------------
# run_metadata
# ---------------------------------------------------------------------------

class RunMetadata(BaseModel):
    prompt_versions: dict[str, str]  # e.g. {"intake": "v3", "validation": "v2"}
    model: str
    llm_calls: int
    latency_ms: int


# ---------------------------------------------------------------------------
# top-level contract
# ---------------------------------------------------------------------------

class BookingIntakeResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"

    message: MessageInfo
    references: References
    routing: Routing
    parties: Parties
    cargo: list[CargoLine]
    documentation: Documentation
    commercial: Commercial
    sources: Sources
    validation: Validation
    clarifications: list[Clarification] = Field(default_factory=list)
    run_metadata: RunMetadata