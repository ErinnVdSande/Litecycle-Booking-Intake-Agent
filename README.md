# Booking Intake Agent

An intake pipeline for freight-forwarding emails: an LLM extracts structured booking data
from free-text messages, a deterministic layer resolves that data against master data,
booking history, and the agent's own prior processing, and a validation agent decides
whether a booking is ready, needs a human look, or is blocked.

---

## Starten

### Setup (under 5 minutes)

```bash
cd App
chmod +x install.sh run.sh      # git often strips the executable bit on clone
./install.sh                     # creates .venv, installs requirements.txt,
                                  # copies .env.example -> .env
```

Open `App/.env` and fill in your real key:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

Then run the pipeline against the 5 sample emails in `Examples/`:

```bash
./run.sh
```

This processes every `.txt` file in `Examples/`, writes one result JSON per message to
`outputs/`, a structured state-transition log to `outputs/run_log.jsonl`, and the agent's
own derived state to `outputs/agent_memory.json`. A summary table prints at the end.

### Frontend (optional, for reviewing results visually)

```bash
cd Frontend
chmod +x run.sh
./run.sh                         # npm install (first run only) + npm run dev
```

Opens a local dev server (usually `http://localhost:5173`). Use the "Load results" button
to point it at the `outputs/*.json` files produced by the backend run above — see
Architectuur for why this is a file-load step rather than a live API connection.

### Notebook (design log, not required to run the pipeline)

`Notebook/booking_intake_agent_notebook.ipynb` is the design log — assumptions,
per-phase testing, and the golden dataset live there. Not the runtime; the actual logic is
entirely in `App/`.

### AI tools used, and what I personally reviewed

I used Claude (Anthropic) extensively throughout — as a coding assistant for the matcher/
enrichment/validation/orchestration logic, for design discussion (e.g. weighing tiered
extraction vs. LLM-only, fuzzy-matching scope, the agent-memory design), and for debugging
against real output.

**I read and reviewed every file myself before treating it as done** — every module in
`App/`, every fix, every test. The AI tool wrote code; I'm the one who understands what's
actually running and why, and I made the calls on architecture and scope throughout (the
tiered-vs-LLM-only pivot, the agent-memory design, what stays deterministic vs. what the LLM
handles). A few concrete examples of that review actually catching things, beyond just
reading code line by line:
- **Read the actual sample email text** (§6.1) directly rather than trusting an invented
  example — this is how the `Datum:` header format bug (Dutch weekday name + time, which
  broke the first date-parsing regex) got caught, since it only showed up against real text.
- **Ran the real 5 messages against the live API twice** (before and after this session's
  fixes) and diffed the actual output against a hand-derived golden dataset field by field —
  not just "the code runs without errors." One misprediction of my own was caught this way
  (M-003's `payable` — see Aannames) and corrected.
- **Traced the actual bug root causes myself** before accepting a fix — e.g. confirmed that
  `token_set_ratio` genuinely scores "CX" vs "CX Service" as 100 by design (not a bug) before
  accepting that a different metric was needed for confidence scoring; confirmed the
  precedent-matcher self-referencing bug (matching a message against its own eventual
  outcome) by checking `copied_from` in the booking history data, not by trusting the
  explanation on its own.
- **Verified the prompt-injection defense against the real injected text** in M-005 (not a
  synthetic test) — confirmed `payable`/`quotation_ref`/`status` did not comply with the
  injected instructions, using the actual `sample5.json` output, not an assumption.

---

## Aannames

Assumption, reason, and the question I'd have wanted to ask — in the order they came up.

| Assumption | Reason | Question I'd have asked |
|---|---|---|
| Extraction is LLM-only, not the originally-planned tiered pipeline (regex → gazetteer → rule-based classifier → LLM residual) | Prototyped a tiered/spaCy-based version first. Structural segmentation (splitting an email into cargo lines, party blocks, etc.) proved too fragile across the 5 samples' inconsistent formatting — M-002 in particular has no block structure at all. This is a data-volume problem, not a dead end: spaCy's `EntityRuler`/`Matcher` approach, or a trained NER model, is genuinely viable once there's enough real historical email volume to (a) learn the actual range of formatting variation rather than guessing from 5 examples, and (b) have enough labeled data to train a statistical model rather than hand-write segmentation rules. Deterministic logic (gazetteer, precedent, voyage, validation) stayed out of the LLM regardless of this pivot. | Are real production emails from this client base more consistently formatted than these 5, or is variability like M-002 the norm? How much historical email volume exists to train against, if the spaCy route were revisited later? |
| Fuzzy string matching (RapidFuzz) is used only for genuinely open-vocabulary text; anything with a curated alias table resolves via exact lookup first | Character-level similarity is the wrong tool for known variants — "Gand"/"Gent" score ~50% similar despite being an exact listed alias. Two early implementation attempts used raw fuzzy matching for ports/services directly and had to be corrected. | Is the relation/port/service alias data in `Db/db.json` a complete list, or should I expect unlisted variants in production? |
| Booker inference from sender address + signature is a deliberate exception to "extract only what's stated" | None of the 5 emails have an explicit "Booker:" label — it's only identifiable from the `Van:` header and signature block. A blanket "never infer" instruction was silently causing this field to come back empty, breaking precedent matching downstream. | Is there ever a case where the sender is NOT the booker (e.g. someone CC'd handling the booking on behalf of the actual customer)? |
| When a booker's relation record has role "booker en shipper" and no separate shipper is stated, shipper defaults to booker | M-002 states no shipper anywhere, but Orchard Produce's relation role says they're both. Sourced as MASTER_DATA at full confidence, not guessed. | Should this also apply symmetrically for a client who books via a third party? |
| Precedent matching requires a message_date cutoff, excluding same-day-or-later bookings | Without one, the matcher can match a message against a booking record that IS that message's own eventual outcome (booking 4471902's own `copied_from` field points to 4468731). | Is the booking table ever backfilled out of order in real SEAFLOW data? |
| A bl_instruction referencing a carrier_booking_no looks up routing/voyage from the agent's own memory first, then a static booking-table fallback — and does NOT guess a "best voyage available now" if only a route is found | M-004 doesn't restate the route at all. Guessing a current best-available voyage for an old confirmed booking risks presenting a different sailing than what was actually booked, since capacity/closing dates have moved on. | Does SEAFLOW's own system ever expose the originally-booked voyage directly, making this reconciliation unnecessary in production? |
| An other-classified message (M-003) gets scoped validation — no cargo/route/voyage checks | M-003 was never trying to state cargo or a route. Running the full booking-validation gauntlet produced misleading errors for a message that isn't a booking attempt. | Are there other other-classified message types this scoping would be wrong for? |
| The no-precedent → relation.default_payable fallback isn't scoped by message classification, so it also fires for other messages | Discovered via a real run, not designed intentionally — M-003 came back with payable="PRP" even though it's a reconciliation note. Left as-is (harmless), flagged as unreviewed rather than a considered choice. | Should enrichment (not just validation) be scoped down for other-classified messages? |
| etd_hint parsing ("24/6") assumes the message's own year, rolling to next year if the result is in the past relative to the message | No sample tests a year-boundary case directly, but freight bookings are made close to departure. | Are there realistic long-lead-time cases where this rollover logic would misfire? |
| Orchestration retries apply only to the LLM intake call | Every other step is deterministic — a failure means a real bug or bad input, and retrying identical deterministic code won't produce a different result. | Should enrichment/validation failures instead be retried, in case of a transient external-dependency issue rather than a code bug? |
| Prompt-injection defense is two-layered: system prompt treats email content as data, and a validation canary scans extracted field values for suspicious phrases | Tested directly against M-005's real injected text — the LLM did not comply, and the canary never fired because nothing was extracted into any field for it to catch. This has been resolved to also test on the raw email after extraction. | How do we stay up to date on prompt injection? |

---

## Architectuur

### Agents

- **Intake agent** (`App/agents/intake_agent.py`) — one structured-output LLM call
  (LangChain + `langchain-openrouter`, Claude Sonnet), narrower than the final schema: it
  only extracts what a single email can state (`MESSAGE`-sourced fields). No master-data
  resolution, no business rules — those are deliberately kept downstream.
- **Enrichment** (`App/enrichment/enrichment.py`) — not an LLM agent; a deterministic
  pipeline that resolves the intake agent's raw extraction against master data
  (`App/matchers/gazetteer.py`), booking precedent (`App/matchers/precedentMatcher.py`),
  voyage availability (`App/matchers/voyageMatcher.py`), and the agent's own prior
  processing (`App/memory/agent_memory.py`).
- **Validation agent** (`App/agents/validation_agent.py`) — rule-based, one method per
  check, deriving a final `ready` / `needs_review` / `blocked` status from issue severities.
- **Orchestrator** (`App/agents/orchestrator.py`) — a hand-rolled, explicit state machine
  (`received → date_extracted → intake_extracted → enriched → validated →
  ready/needs_review/blocked`), not a generic agent framework. The state list is visible and
  inspectable, matching §7's ask for explicit states/transitions rather than something
  hidden inside a framework's internals.

### Where there is, and isn't, an LLM — and why

The intake agent is the **only** component that calls an LLM. Everything downstream —
gazetteer resolution, precedent matching, voyage selection, business-rule validation — is
plain Python, testable in isolation, and cannot hallucinate outside an allow-list.

This split follows directly from what each problem actually is:

- **Party names, addresses, goods descriptions, and the "next available vs. specific date"
  distinction are genuinely open-vocabulary language understanding.** An LLM is the
  pragmatic choice — there's no fixed vocabulary to look up against.
- **Port codes, service codes, commodity codes, and company-name synonyms are closed,
  curated vocabularies** (`Db/db.json`'s `port`/`service`/`relation` tables). These resolve
  via exact lookup first, with fuzzy string matching only as a fallback for genuine typos
  not in the alias table — never as the primary mechanism for a known variant. (Two early
  implementation passes reached for fuzzy matching here directly and both had to be
  corrected — see Aannames.)
- **Precedent selection and voyage selection are rule-based ranking over structured
  records**, not language tasks — booker/shipper matching, route matching, and recency are
  exact/near-exact comparisons the LLM never needs to be involved in.
- **Business-rule validation is exactly the kind of thing that must be reproducible and
  auditable** — a model re-deciding this per run would make two runs of the identical
  message potentially disagree, which is unacceptable for a validation status.

### Structured output & harnessing (§7)

- **Schema enforcement**: the intake agent uses the provider's native structured-output
  mode (`with_structured_output`) against a Pydantic model — not a JSON-mode prompt
  instruction. `App/schema/schema.py` is the single source of truth for the final contract;
  the intake agent's own extraction schema is deliberately narrower (only `MESSAGE`-sourced
  fields), and enrichment fills in everything else.
- **Prompt versioning**: `run_metadata.prompt_versions` in every result JSON records which
  prompt version produced it (`intake-v1` → `intake-v2` after the booker-inference fix —
  see Aannames). Prompts live as module-level constants in `intake_agent.py`, not inline
  strings scattered through business logic.
- **Untrusted input**: the intake agent's system prompt explicitly instructs the model to
  treat email content as data, never as instructions, and the email is wrapped in `<email>`
  tags to reinforce that boundary. Tested against the real injected text in M-005 (see
  Starten) — confirmed the model did not comply.
- **Allow-lists**: port codes, commodity codes, and message classifications are `Literal`
  types / enums in the Pydantic schema itself, not just validated after the fact. An
  unresolvable port comes back as `code: null` rather than an invalid value, and the
  validation agent flags that explicitly (`UNRESOLVED_PORT_OF_LOADING`, `error` severity).
- **Failures**: `run_intake_agent` distinguishes a parsing failure (model responded outside
  the schema) from an API-level failure (timeout, rate limit, network error). The
  orchestrator retries only the LLM call (up to 2 retries, 3 attempts total) with a fixed
  backoff; every other pipeline step is deterministic and fails immediately on error rather
  than retrying something that can't succeed differently the second time.

### Agent memory — a design decision, not an afterthought

`App/memory/agent_memory.py` is a separate store from `Db/db.json`, on purpose: `Db/db.json`
is curated reference data (checked into git, seeded once); the memory store is the agent's
own runtime-generated state (written on every `booking_request`, freely resettable, and
already covered by the `outputs/` `.gitignore` entry with zero new config). It exists
specifically because a `bl_instruction` referencing an existing booking doesn't restate the
route — the agent needs to recall what it itself determined earlier, not re-derive it from
nothing. Verified end-to-end against the real 5 messages: `agent_memory.json` shows the
customer_reference↔carrier_booking_no reconciliation happening live (see Aannames).

### Frontend

A standalone React (Vite) app (`Frontend/`), not connected to a live backend — deliberately.
The pipeline is a batch CLI tool (`App/main.py`), not a server; there's no persistent process
to poll for new results. The frontend loads whichever `outputs/*.json` files you point it at,
which matches how the pipeline is actually meant to be run (batch-process a set of emails,
then review the results) rather than implying a real-time system that doesn't exist.

---

## Productie

### Where human intervention is mandatory

- **Any `blocked` status** — by definition, the validation agent found a hard stop
  (unresolved port, no cargo, no voyage available, missing required field). No auto-booking
  should ever happen on a `blocked` result.
- **Any `needs_review` status** — softer, but still requires a human glance; typically a
  fuzzy match, a defaulted value with no real source, or a low classification confidence.
- **Every low-confidence `sources` entry**, regardless of overall status — `sources` exists
  specifically so a human reviewer can see *which* fields to check, not re-verify everything.
- **Any message where the injection-marker canary fires** — even if the LLM didn't comply,
  a message containing an embedded instruction is worth a human look at the sender
  relationship, not just silent processing.
- **Any `other`-classified message** — by design, no autonomous booking action is taken on
  these; they're surfaced for a human to read and act on manually.

### Personal data handling

Emails routinely contain names, direct phone numbers, and in M-004's case a VAT/company
registration number. Before this runs against a client's real mailbox:

- **The LLM provider receives full email content on every call** — this needs an explicit
  data-processing agreement covering the client's own customer data, since shipper/
  consignee/notify parties are third parties to the contract between the client and
  Litecycle, not just Litecycle's own data.
- **`outputs/*.json` and `outputs/agent_memory.json` currently persist full extracted party
  details in plaintext on disk**, with no retention policy, no encryption at rest, and no
  access control beyond filesystem permissions — acceptable for local development, not for
  production without at minimum a defined retention window, encryption at rest, and
  role-based access.
- **`run_log.jsonl` and `agent_memory.json` are excluded from git** (`.gitignore`), which is
  necessary but not sufficient — they shouldn't sit indefinitely on a developer's machine
  once a message is fully processed.
- **No redaction or minimization happens anywhere in the pipeline** — full address blocks,
  phone numbers, and VAT/registration numbers flow straight through to the final JSON.
  Worth deciding whether downstream consumers actually need the full address block.

### What I would monitor in production

- **Per-message outcome distribution** (`ready` / `needs_review` / `blocked` / orchestration
  `failed`) over time — a spike in `blocked`/`failed` is the fastest signal something
  upstream changed.
- **LLM call latency and failure rate**, partially captured in `run_metadata.latency_ms` and
  the orchestrator's retry-attempt count already — needs aggregation and alerting on top.
- **Precedent-match confidence distribution** — a rising share of low-confidence/no-precedent
  results could mean more genuinely new clients (fine) or an extraction-quality regression
  (not fine) — these look identical without a trend to compare against.
- **Classification accuracy in practice** — comparing the pipeline's classification against
  what a human reviewer later confirms (no feedback loop currently exists) would be the real
  signal for classifier drift, beyond the static golden-dataset check.
- **Injection-marker canary firing rate** — tracking this even at zero establishes a
  baseline, so a sudden nonzero rate is immediately visible.

### What the booking desk sees if the provider has an outage

Currently: the orchestrator retries the LLM call up to 2 times with a fixed 1-second
backoff, then returns `success=False`, `final_state=FAILED`, and an `error` string — logged
to `run_log.jsonl`, but **no result JSON is written for that message at all**, since there's
no valid result to construct without extraction having succeeded.

Not sufficient for production as-is:
- **No notification path** — a failed message is discoverable only by checking
  `run_log.jsonl` or noticing a missing output file. In production this needs to actively
  surface (the message should stay visibly "pending" in whatever queue view the desk uses,
  not silently vanish from the batch).
- **No automatic retry-later / dead-letter queue** — a message that failed due to a
  transient provider outage just stays failed; it needs re-attempting once the provider
  recovers, not a manual re-run.
- **The current retry cap is tuned for "malformed output this once," not "the provider is
  down for 20 minutes"** — worth separating into two failure classes with different retry
  strategies (fast retry for malformed response, slower backoff-and-alert for connection
  failure).
- **The booking desk needs a clear distinction between "this message needs a human
  decision" (`blocked`/`needs_review` — pipeline worked, content needs judgment) and "the
  pipeline itself is down" (`FAILED` — nothing to review yet).** These produce very
  differently-shaped output today (a full result JSON vs. nothing), which is the right
  instinct, but the frontend has no view for `FAILED` messages at all yet.

### Other things not yet production-ready

- Golden dataset is 5 messages, hand-derived and empirically checked once — not a
  statistically meaningful eval set. Needs real historical messages (anonymized) first.
- No load/concurrency testing — `main.py` processes messages sequentially; unclear how the
  TinyDB-backed master data and memory store behave under concurrent access.
- Frontend has no authentication and isn't deployed anywhere — local-dev-only as it stands.
