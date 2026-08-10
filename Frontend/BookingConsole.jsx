import React, { useState, useCallback } from "react";
import {
  Anchor, Ship, AlertTriangle, CheckCircle2, XCircle, ChevronDown, ChevronRight,
  Package, MapPin, Users, FileText, Receipt, Braces, Circle, Upload, RotateCcw,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Demo data — shaped exactly like BookingIntakeResult.model_dump(). This is
// the fallback shown before you load real files; use the "Load results"
// button to point this at your actual App/outputs/*.json files (from
// `App/run.sh`) — no backend needed, this reads them directly in-browser.
// ---------------------------------------------------------------------------

const DEMO_RESULTS = [
  {
    message: { message_id: "M-001", classification: "booking_request", classification_confidence: 0.96, language: "nl" },
    references: {
      carrier_booking_no: null, customer_reference: "DF-026-00604",
      precedent_booking_no: "4468731", precedent_reason: "match op booker, shipper en route",
    },
    routing: {
      port_of_loading: { raw: "Gent", code: "BEGNE" },
      port_of_discharge: { raw: "Georgetown", code: "GYGEO" },
      requested_departure: { mode: "next_available", service_or_vessel_raw: null, etd_hint: null },
      selected_voyage: {
        voyage_code: "CX2614", vessel: "CORAL TRADER", ets_pol: "2026-06-26", eta_pod: "2026-07-14",
        selection_reason: "eerste afvaart na boekingsdatum met voldoende capaciteit", alternatives: ["CX2615"],
      },
    },
    parties: {
      shipper: { name: "Kalico Minerals and Services Limited", address_lines: ["London W4 5RP United Kingdom"], country: "United Kingdom" },
      consignee: { name: "KALICO GUYANA INC.", address_lines: ["Lot 42 Ogle Industrial Estate"], country: "Guyana" },
      notify: null,
      booker: { name: "Delmar Forwarding B.V.", address_lines: [], country: "Netherlands" },
    },
    cargo: [{
      line_no: 1, packages: { count: 160, type: "Pallets" },
      inner_packages: { count: 3318, unit: "bags", description: "50lb bags" },
      goods_description: "BARIFLOW HD", gross_weight_kg: 80432, weight_basis: "stated",
      hs_code: "2511110", commodity_code: "999",
    }],
    documentation: { bl_type: null, marks_and_numbers: null, shipment_number: null },
    commercial: { payable: "PRP", quotation_ref: "301" },
    sources: {
      gross_weight_kg: { source: "MESSAGE", confidence: 0.9, evidence: "160 Pallets - 80.432 kg." },
      packages: { source: "MESSAGE", confidence: 0.9, evidence: "160 Pallets" },
      port_of_loading: { source: "MASTER_DATA", confidence: 1.0, evidence: "Gent -> Gent (BEGNE)" },
      selected_voyage: { source: "DERIVED", confidence: 0.85, evidence: "eerste afvaart na boekingsdatum met voldoende capaciteit" },
      commodity_code: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4468731" },
      payable: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4468731" },
      quotation_ref: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4468731" },
      shipper: { source: "MESSAGE", confidence: 0.9, evidence: "Kalico Minerals and Services Limited" },
    },
    validation: { status: "ready", issues: [] },
    run_metadata: { prompt_versions: { intake: "intake-v1" }, model: "anthropic/claude-sonnet-4-5", llm_calls: 1, latency_ms: 1180 },
  },
  {
    message: { message_id: "M-002", classification: "booking_request", classification_confidence: 0.71, language: "nl" },
    references: { carrier_booking_no: null, customer_reference: null, precedent_booking_no: "4467882", precedent_reason: "match op booker, shipper en route" },
    routing: {
      port_of_loading: { raw: "Gent", code: "BEGNE" },
      port_of_discharge: { raw: "Paramaribo", code: "SRPBM" },
      requested_departure: { mode: "specified", service_or_vessel_raw: "CX Service", etd_hint: "2026-06-24" },
      selected_voyage: {
        voyage_code: "SA1122", vessel: "NORTHERN DAWN", ets_pol: "2026-06-24", eta_pod: "2026-07-09",
        selection_reason: "dichtst bij gevraagde datum 2026-06-24 met voldoende capaciteit; let op: gevraagde dienst 'CX Service' komt niet voor op deze route; dienst SA Service gebruikt",
        alternatives: ["SA1123"],
      },
    },
    parties: {
      shipper: { name: "Orchard Produce N.V.", address_lines: [], country: "Belgium" },
      consignee: { name: "Paramaribo Fresh Distributors", address_lines: [], country: "Suriname" },
      notify: null,
      booker: { name: "Orchard Produce N.V.", address_lines: [], country: "Belgium" },
    },
    cargo: [{
      line_no: 1, packages: { count: 60, type: "pallets" }, inner_packages: null,
      goods_description: "uien", gross_weight_kg: 75000, weight_basis: "stated",
      hs_code: null, commodity_code: "401",
    }],
    documentation: { bl_type: null, marks_and_numbers: null, shipment_number: null },
    commercial: { payable: "COL", quotation_ref: "412" },
    sources: {
      gross_weight_kg: { source: "MESSAGE", confidence: 0.85, evidence: "60 pallets uien" },
      packages: { source: "MESSAGE", confidence: 0.85, evidence: "60 pallets" },
      port_of_loading: { source: "MASTER_DATA", confidence: 1.0, evidence: "Gent -> Gent (BEGNE)" },
      selected_voyage: { source: "DERIVED", confidence: 0.51, evidence: "dienst-mismatch: CX gevraagd, SA gebruikt" },
      commodity_code: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4467882" },
      payable: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4467882" },
      quotation_ref: { source: "PRECEDENT", confidence: 0.90, evidence: "boeking 4467882" },
      shipper: { source: "MESSAGE", confidence: 0.85, evidence: "Orchard Produce N.V." },
    },
    validation: {
      status: "needs_review",
      issues: [
        { code: "LOW_SOURCE_CONFIDENCE", severity: "warning", field: "sources.selected_voyage", message: "Lage betrouwbaarheid (0.51) voor veld 'selected_voyage': dienst-mismatch: CX gevraagd, SA gebruikt", suggested_action: "Handmatig verifiëren." },
        { code: "LOW_CLASSIFICATION_CONFIDENCE", severity: "warning", field: "message.classification_confidence", message: "Lage classificatiezekerheid (0.71) voor type 'booking_request'.", suggested_action: "Handmatig bevestigen dat de classificatie klopt." },
      ],
    },
    run_metadata: { prompt_versions: { intake: "intake-v1" }, model: "anthropic/claude-sonnet-4-5", llm_calls: 1, latency_ms: 1340 },
  },
  {
    message: { message_id: "M-005", classification: "booking_request", classification_confidence: 0.93, language: "en" },
    references: { carrier_booking_no: null, customer_reference: null, precedent_booking_no: null, precedent_reason: "Geen precedent gevonden" },
    routing: {
      port_of_loading: { raw: "Gent", code: "BEGNE" },
      port_of_discharge: { raw: "Georgetown", code: "GYGEO" },
      requested_departure: { mode: "next_available", service_or_vessel_raw: null, etd_hint: null },
      selected_voyage: {
        voyage_code: "CX2614", vessel: "CORAL TRADER", ets_pol: "2026-06-26", eta_pod: "2026-07-14",
        selection_reason: "eerste afvaart na boekingsdatum met voldoende capaciteit", alternatives: ["CX2615"],
      },
    },
    parties: {
      shipper: { name: "Ferro Meteren B.V.", address_lines: [], country: "Netherlands" },
      consignee: { name: "Demerara Agri Supplies Ltd", address_lines: [], country: "Guyana" },
      notify: null,
      booker: { name: "Ferro Meteren B.V.", address_lines: [], country: "Netherlands" },
    },
    cargo: [
      { line_no: 1, packages: { count: 40, type: "big bags" }, inner_packages: null, goods_description: "NPK 15-15-15 fertiliser", gross_weight_kg: 40000, weight_basis: "stated", hs_code: "310520", commodity_code: "999" },
      { line_no: 2, packages: { count: 12, type: "IBC totes" }, inner_packages: null, goods_description: "lubricant additive", gross_weight_kg: 12600, weight_basis: "stated", hs_code: "340399", commodity_code: "999" },
    ],
    documentation: { bl_type: null, marks_and_numbers: null, shipment_number: null },
    commercial: { payable: null, quotation_ref: null },
    sources: {
      gross_weight_kg: { source: "MESSAGE", confidence: 0.9, evidence: "40.000 kg total" },
      packages: { source: "MESSAGE", confidence: 0.9, evidence: "40 big bags" },
      port_of_loading: { source: "MASTER_DATA", confidence: 1.0, evidence: "Gent -> Gent (BEGNE)" },
      selected_voyage: { source: "DERIVED", confidence: 0.85, evidence: "eerste afvaart na boekingsdatum met voldoende capaciteit" },
      commodity_code: null,
      payable: null,
      quotation_ref: null,
      shipper: { source: "MESSAGE", confidence: 0.9, evidence: "Ferro Meteren B.V." },
    },
    validation: {
      status: "blocked",
      issues: [
        { code: "NO_PRECEDENT", severity: "warning", field: "references.precedent_booking_no", message: "Geen precedent gevonden", suggested_action: "Commodity code, payable en quotation_ref handmatig controleren — geen precedent beschikbaar om op terug te vallen." },
        { code: "COMMODITY_CODE_DEFAULTED", severity: "warning", field: "cargo[1].commodity_code", message: "Commodity code voor cargoregel 1 teruggevallen op standaardwaarde 999 (N.O.S.) zonder bron.", suggested_action: "Controleer of 999 correct is voor deze lading." },
        { code: "COMMODITY_CODE_DEFAULTED", severity: "warning", field: "cargo[2].commodity_code", message: "Commodity code voor cargoregel 2 teruggevallen op standaardwaarde 999 (N.O.S.) zonder bron.", suggested_action: "Controleer of 999 correct is voor deze lading." },
        { code: "MISSING_PAYABLE", severity: "error", field: "commercial.payable", message: "Geen payable-terms bekend (nieuwe klant, geen standaardwaarde in relatietabel).", suggested_action: "Operator moet payable-terms bevestigen voor deze klant." },
      ],
    },
    run_metadata: { prompt_versions: { intake: "intake-v1" }, model: "anthropic/claude-sonnet-4-5", llm_calls: 1, latency_ms: 1510 },
  },
];

// ---------------------------------------------------------------------------
// Visual tokens
// ---------------------------------------------------------------------------

const STATUS_STYLE = {
  ready: { dot: "bg-emerald-500", text: "text-emerald-700", bg: "bg-emerald-50", border: "border-emerald-200", label: "Klaar" },
  needs_review: { dot: "bg-amber-500", text: "text-amber-700", bg: "bg-amber-50", border: "border-amber-200", label: "Controle nodig" },
  blocked: { dot: "bg-rose-500", text: "text-rose-700", bg: "bg-rose-50", border: "border-rose-200", label: "Geblokkeerd" },
};

const SOURCE_STYLE = {
  MESSAGE: { label: "BER", color: "bg-slate-700 text-slate-100" },
  PRECEDENT: { label: "PREC", color: "bg-indigo-600 text-indigo-50" },
  MASTER_DATA: { label: "MDATA", color: "bg-teal-600 text-teal-50" },
  DERIVED: { label: "DERIV", color: "bg-amber-600 text-amber-50" },
  SYSTEM: { label: "SYS", color: "bg-slate-400 text-slate-50" },
};

const SEVERITY_STYLE = {
  error: { icon: XCircle, text: "text-rose-700", bg: "bg-rose-50", border: "border-rose-200" },
  warning: { icon: AlertTriangle, text: "text-amber-700", bg: "bg-amber-50", border: "border-amber-200" },
  info: { icon: Circle, text: "text-slate-500", bg: "bg-slate-50", border: "border-slate-200" },
};

// ---------------------------------------------------------------------------
// Small building blocks
// ---------------------------------------------------------------------------

function ConfidenceBar({ value }) {
  const pct = Math.round(value * 100);
  const color = value >= 0.85 ? "bg-teal-500" : value >= 0.65 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="flex items-center gap-1.5 w-20 shrink-0">
      <div className="h-1 flex-1 rounded-full bg-slate-200 overflow-hidden">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[10px] font-mono text-slate-400 w-7 text-right">{pct}</span>
    </div>
  );
}

function SourceTag({ entry }) {
  if (!entry) {
    return <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-400 border border-dashed border-slate-300">geen bron</span>;
  }
  const style = SOURCE_STYLE[entry.source] || SOURCE_STYLE.SYSTEM;
  return (
    <div className="flex items-center gap-2" title={entry.evidence}>
      <span className={`text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded ${style.color}`}>{style.label}</span>
      <ConfidenceBar value={entry.confidence} />
    </div>
  );
}

function Field({ label, value, source, mono }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-slate-100 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-0.5">{label}</div>
        <div className={`text-sm text-slate-800 truncate ${mono ? "font-mono" : ""}`}>{value ?? <span className="text-slate-300 italic">—</span>}</div>
      </div>
      {source !== undefined && <SourceTag entry={source} />}
    </div>
  );
}

function Section({ icon: Icon, title, children }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden bg-white">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2.5 bg-slate-50 hover:bg-slate-100 transition-colors text-left"
      >
        {open ? <ChevronDown size={14} className="text-slate-400" /> : <ChevronRight size={14} className="text-slate-400" />}
        <Icon size={14} className="text-slate-500" />
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-600">{title}</span>
      </button>
      {open && <div className="px-4 py-1">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main detail panel
// ---------------------------------------------------------------------------

function DetailPanel({ result }) {
  const [showRaw, setShowRaw] = useState(false);
  const status = STATUS_STYLE[result.validation.status];

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50">
      <div className="max-w-3xl mx-auto px-8 py-8">
        {/* Header */}
        <div className="flex items-start justify-between mb-6">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h1 className="text-xl font-semibold text-slate-900 font-mono">{result.message.message_id}</h1>
              <span className={`inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${status.bg} ${status.text} ${status.border}`}>
                <Circle size={6} className={`${status.dot} rounded-full fill-current`} />
                {status.label}
              </span>
            </div>
            <div className="text-sm text-slate-500">
              {result.message.classification.replace("_", " ")} · zekerheid {Math.round(result.message.classification_confidence * 100)}% · {result.message.language}
            </div>
          </div>
          <div className="text-right text-xs text-slate-400 font-mono">
            {result.run_metadata.model}<br />
            {result.run_metadata.llm_calls} call · {result.run_metadata.latency_ms}ms
          </div>
        </div>

        {/* Validation findings */}
        {result.validation.issues.length > 0 && (
          <div className="mb-6 space-y-2">
            {result.validation.issues.map((issue, i) => {
              const sev = SEVERITY_STYLE[issue.severity];
              const Icon = sev.icon;
              return (
                <div key={i} className={`flex gap-3 p-3 rounded-lg border ${sev.bg} ${sev.border}`}>
                  <Icon size={16} className={`${sev.text} mt-0.5 shrink-0`} />
                  <div className="min-w-0">
                    <div className={`text-sm font-medium ${sev.text}`}>{issue.message}</div>
                    <div className="text-xs text-slate-500 mt-0.5">{issue.suggested_action}</div>
                    <div className="text-[10px] font-mono text-slate-400 mt-1">{issue.code} · {issue.field}</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Field sections */}
        <div className="space-y-3">
          <Section icon={MapPin} title="Routing">
            <Field label="Laadhaven" value={`${result.routing.port_of_loading.raw} (${result.routing.port_of_loading.code ?? "?"})`} source={result.sources.port_of_loading} mono />
            <Field label="Loshaven" value={`${result.routing.port_of_discharge.raw} (${result.routing.port_of_discharge.code ?? "?"})`} mono />
            {result.routing.selected_voyage ? (
              <Field
                label="Geselecteerde afvaart"
                value={`${result.routing.selected_voyage.voyage_code} · ${result.routing.selected_voyage.vessel} · ETS ${result.routing.selected_voyage.ets_pol}`}
                source={result.sources.selected_voyage}
                mono
              />
            ) : (
              <Field label="Geselecteerde afvaart" value={null} source={null} />
            )}
          </Section>

          <Section icon={Users} title="Partijen">
            <Field label="Shipper" value={result.parties.shipper.name} source={result.sources.shipper} />
            <Field label="Consignee" value={result.parties.consignee.name} />
            <Field label="Booker" value={result.parties.booker.name} />
          </Section>

          <Section icon={Package} title={`Cargo (${result.cargo.length} regel${result.cargo.length > 1 ? "s" : ""})`}>
            {result.cargo.map((line) => (
              <div key={line.line_no} className="py-2 border-b border-slate-100 last:border-0">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="text-[11px] uppercase tracking-wide text-slate-400 mb-0.5">Regel {line.line_no} · {line.goods_description}</div>
                    <div className="text-sm text-slate-800 font-mono">
                      {line.packages.count} {line.packages.type} · {line.gross_weight_kg.toLocaleString("nl-BE")} kg · HS {line.hs_code ?? "?"} · commodity {line.commodity_code}
                    </div>
                  </div>
                  {line.line_no === 1 && <SourceTag entry={result.sources.gross_weight_kg} />}
                </div>
              </div>
            ))}
          </Section>

          <Section icon={Receipt} title="Commercieel">
            <Field label="Payable" value={result.commercial.payable} source={result.sources.payable} mono />
            <Field label="Quotation ref" value={result.commercial.quotation_ref} source={result.sources.quotation_ref} mono />
            <Field label="Precedent" value={result.references.precedent_booking_no ?? result.references.precedent_reason} mono={!!result.references.precedent_booking_no} />
          </Section>
        </div>

        {/* Raw JSON toggle */}
        <div className="mt-6">
          <button
            onClick={() => setShowRaw(!showRaw)}
            className="flex items-center gap-2 text-xs font-medium text-slate-500 hover:text-slate-700 transition-colors"
          >
            <Braces size={13} />
            {showRaw ? "Verberg" : "Toon"} volledige JSON
            {showRaw ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </button>
          {showRaw && (
            <pre className="mt-2 p-4 bg-slate-900 text-slate-200 rounded-lg text-[11px] leading-relaxed overflow-x-auto font-mono">
              {JSON.stringify(result, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mail list sidebar
// ---------------------------------------------------------------------------

function MailList({ results, selectedId, onSelect, onLoadFiles, isDemo, onResetDemo, loadError }) {
  return (
    <div className="w-72 shrink-0 border-r border-slate-200 bg-white overflow-y-auto flex flex-col">
      <div className="px-4 py-4 border-b border-slate-200">
        <div className="flex items-center gap-2 text-slate-800">
          <Anchor size={16} />
          <span className="text-sm font-semibold tracking-tight">Booking Intake</span>
        </div>
        <div className="text-[11px] text-slate-400 mt-0.5">
          {isDemo ? "Demo data" : `${results.length} berichten verwerkt`}
        </div>

        <label className="mt-3 flex items-center justify-center gap-1.5 text-xs font-medium text-slate-600 border border-slate-200 rounded-lg px-3 py-2 cursor-pointer hover:bg-slate-50 transition-colors">
          <Upload size={13} />
          Laad resultaten (outputs/*.json)
          <input
            type="file" accept=".json" multiple className="hidden"
            onChange={(e) => onLoadFiles(e.target.files)}
          />
        </label>
        {!isDemo && (
          <button
            onClick={onResetDemo}
            className="mt-1.5 w-full flex items-center justify-center gap-1.5 text-[11px] text-slate-400 hover:text-slate-600 transition-colors"
          >
            <RotateCcw size={11} />
            Terug naar demo data
          </button>
        )}
        {loadError && (
          <div className="mt-2 text-[11px] text-rose-600 bg-rose-50 border border-rose-200 rounded px-2 py-1.5">
            {loadError}
          </div>
        )}
      </div>

      {results.length === 0 ? (
        <div className="flex-1 flex items-center justify-center px-6 text-center">
          <p className="text-xs text-slate-400">
            Geen resultaten geladen. Kies één of meer JSON-bestanden uit <code className="font-mono">App/outputs/</code>.
          </p>
        </div>
      ) : (
        <div>
          {results.map((r) => {
            const status = STATUS_STYLE[r.validation.status];
            const active = r.message.message_id === selectedId;
            return (
              <button
                key={r.message.message_id}
                onClick={() => onSelect(r.message.message_id)}
                className={`w-full text-left px-4 py-3 border-b border-slate-100 transition-colors ${active ? "bg-slate-50" : "hover:bg-slate-50"}`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-mono font-medium text-slate-800">{r.message.message_id}</span>
                  <Circle size={7} className={`${status.dot} rounded-full fill-current`} />
                </div>
                <div className="text-xs text-slate-500 truncate">{r.parties?.shipper?.name || "—"}</div>
                <div className="text-[11px] text-slate-400 mt-0.5">
                  {r.routing?.port_of_loading?.raw || "?"} → {r.routing?.port_of_discharge?.raw || "?"}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export default function BookingConsole() {
  const [results, setResults] = useState(DEMO_RESULTS);
  const [isDemo, setIsDemo] = useState(true);
  const [selectedId, setSelectedId] = useState(DEMO_RESULTS[0].message.message_id);
  const [loadError, setLoadError] = useState(null);

  const selected = results.find((r) => r.message.message_id === selectedId);

  const handleLoadFiles = useCallback((fileList) => {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    setLoadError(null);
    Promise.all(
      files.map(
        (file) =>
          new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
              try {
                resolve(JSON.parse(reader.result));
              } catch (err) {
                reject(new Error(`${file.name}: ongeldige JSON (${err.message})`));
              }
            };
            reader.onerror = () => reject(new Error(`${file.name}: kon bestand niet lezen`));
            reader.readAsText(file);
          })
      )
    )
      .then((parsed) => {
        // basic shape check — every real result needs at least these three
        // top-level keys; skip anything that doesn't look right (e.g. an
        // error.json written for a FAILED orchestration run) rather than
        // crashing the whole load.
        const valid = parsed.filter((r) => r?.message?.message_id && r?.routing && r?.validation);
        const skipped = parsed.length - valid.length;

        if (valid.length === 0) {
          setLoadError("Geen geldige BookingIntakeResult-bestanden gevonden in de selectie.");
          return;
        }

        valid.sort((a, b) => a.message.message_id.localeCompare(b.message.message_id));
        setResults(valid);
        setIsDemo(false);
        setSelectedId(valid[0].message.message_id);
        if (skipped > 0) {
          setLoadError(
            `${skipped} bestand(en) overgeslagen (geen geldig resultaat — waarschijnlijk een *.error.json van een mislukte run).`
          );
        }
      })
      .catch((err) => setLoadError(err.message));
  }, []);

  const handleResetDemo = () => {
    setResults(DEMO_RESULTS);
    setIsDemo(true);
    setSelectedId(DEMO_RESULTS[0].message.message_id);
    setLoadError(null);
  };

  return (
    <div className="flex h-screen bg-white font-sans" style={{ fontFamily: "'IBM Plex Sans', system-ui, sans-serif" }}>
      <MailList
        results={results} selectedId={selectedId} onSelect={setSelectedId}
        onLoadFiles={handleLoadFiles} isDemo={isDemo} onResetDemo={handleResetDemo}
        loadError={loadError}
      />
      {selected && <DetailPanel result={selected} />}
    </div>
  );
}