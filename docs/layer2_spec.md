# Layer 2 Specification — Weekly OI + Flow + Events Analysis

Drafted 2026-04-24. Freezes once user signs off. Changes post-freeze go through a spec amendment; do not edit silently.

## 1. Goal

Every Friday after close, produce a desk-readable markdown note synthesising three data streams over the past trading week, segmented by macro-event boundaries:

1. **CME OI snapshots** — daily settle open-interest per strike, per expiry, per call/put.
2. **Street flow intel** — what executing brokers and pit brokers told us paper has been doing.
3. **Desk client trades** — what KCP actually executed for clients.

The note is shared with the desk. It answers: where is open interest being built or unwound, how do paper's expressed trades correlate with OI moves, how did positioning shift around this week's macro events, and how does what we did for clients fit into the broader picture.

## 2. Non-goals for v2

- Real-time or intraday analysis — this is a weekly product.
- Non-SR3 products. Euribor (ER) and SONIA (SFI) are deferred to post-Bloomberg.
- Weeklies, 2Y/3Y/4Y/5Y mid-curves. v2 covers SR3 quarterlies + 0Q (1Y mid-curve) only.
- Trade recommendations. The tool describes; the desk decides.
- Automatic CME file download. Desk downloads manually each day for v2.

## 3. Cadence

**Daily capture, weekly synthesis.**

- Every trading day EOD: one of the desk downloads the CME VoI file, drops it in `data/oi/daily/YYYY-MM-DD.xls`. The loader parses it into `data/oi/daily/YYYY-MM-DD.json` (structured digest; no LLM call). Cheap, boring, scriptable later.
- Every Friday EOD: the user clicks "Generate weekly rundown" in the app. The pipeline loads 5 daily digests + flow Excel + client-trades Excel + events (FMP API + FOMC scraper if applicable), runs the pre-aggregator to build the event-segmented structured digest, makes **one** `claude -p` call, emits a markdown rundown.

Rationale: aggressively lower cost than daily analysis; noise washes out across 5 days; the cross-stream join is weekly by nature (desk flow log + client trades are weekly cadence anyway).

## 4. Product scope

v2 covers **SR3 + 0Q**, focused on the quarterly futures and all the option chains that settle into them.

### 4.1 Futures (underlying)

CME lists futures for many months, but only the **quarterlies** carry meaningful liquidity and desk positioning — those are the only futures we track:

- 2026: H6 (Mar), M6 (Jun), U6 (Sep), Z6 (Dec)
- 2027: H7, M7, U7, Z7

Non-quarterly futures (FEB, APR, MAY, JUL, AUG, OCT, NOV) are present in the CME file but ignored by the loader.

### 4.2 Option chains loaded

For each quarterly we care about, we load **all three option expiries that settle into it** — the two intervening monthlies and the quarterly itself:

| Quarterly cycle | Monthlies + quarterly (chronological) |
|---|---|
| H6 (Mar 2026) | F6, G6, H6 |
| M6 (Jun 2026) | J6, K6, M6 |
| U6 (Sep 2026) | N6, Q6, U6 |
| Z6 (Dec 2026) | V6, X6, Z6 |
| H7, M7, U7, Z7 | same pattern, 2027 |

Total: **24 option chains per product × 2 products (SR3 + 0Q) = 48 chains**.

CME files all of these under two `OPTION TYPE:` headings: `American Options` (SR3 whites = the quarterlies + their monthlies) and `1 Year Mid-Curve Options` (0Q, same structure). The loader loads every expiry sub-block found under those two headings.

Everything else is filtered out: weeklies, 2Y-5Y mid-curves, First/Second-quarter monthly variants (different products, also out of scope).

### 4.3 Aggregation behaviour: keep monthlies separate

Monthly option chains are **always kept separate in the structured digest** — each monthly expiry has its own `per_expiry` block in §10. This preserves precision: V6 (front-month, fast theta) and Z6 (back-quarterly, real positioning) are different beasts even though they share the Z6 future.

**The LLM is free to roll up to the parent cycle when describing themes** — "paper building Z6 upside" is fine narrative even if the actual data lives in V6 + X6 + Z6 separately. Roll-up is a narrative choice, not a data transformation. The system prompt explicitly permits it (§12).

Underlying quarterly is resolved via `products.underlying_quarterly()` from Layer 1.

## 5. Data sources

### 5.1 CME VoI daily file

- Source: CME website, public Volume & Open Interest report for Interest Rate Options.
- Format: legacy binary `.xls` (OLE), single sheet `VOI Details Report`, ~2300 rows.
- Shape: top **`Futures` section** (one row per future month) followed by ~29 `OPTION TYPE:` sections, each with per-expiry `MMM YY Calls` / `MMM YY Puts` sub-blocks of per-strike rows.

**From the futures section** (top of file), we capture **futures settle price and daily change per quarterly future** (H/M/U/Z only — non-quarterlies ignored per §4.1). Columns: `Month | Globex | Open OutCry | Clear Port | Total Volume | Block Trades | EFP | EFR | TAS | Deliveries | At Close | Change`. We use **At Close** (futures settle) and **Change** (daily move in price terms; positive = rates lower).

**From the option-type sections**, per strike row, we capture: `Strike | Globex | Open OutCry | Clear Port | Total Volume | Block Trades | EOO | Exercises | At Close | Change`. We care about **Total Volume** (today's volume), **At Close** (end-of-day OI), **Change** (ΔOI vs prior day — already computed by CME, saves us a diff).

- Strike format: 4-digit implied-decimal integer (`9631` = 96.31).
- Trading date: not in the file; taken from the filename (`YYYY-MM-DD.xls`).
- Storage: raw file `data/oi/daily/YYYY-MM-DD.xls`, parsed digest `data/oi/daily/YYYY-MM-DD.json`. Digest contains both the per-strike option rows AND the per-quarterly futures settles.

### 5.2 Flow Excel — street intel

- Source: shared OneDrive. Desk maintains throughout the week.
- One row per flow observation. Format the desk commits to:

```
date | raw_note                                      | product | expiry | structure          | size | direction | price
-----|-----------------------------------------------|---------|--------|--------------------|------|-----------|------
...  | SFRU6 96.43/96.50 cs ppr bought 5k at 1       | SR3     | U6     | 96.43/96.50 cs     | 5000 | bought    | 1
```

The `raw_note` column is the pit-broker message verbatim. The remaining columns are a structured parse the desk fills in. Loader is tolerant: if structured columns are empty, the LLM still sees `raw_note` and can reason over it; if they're filled, the pre-aggregator uses them for joins with OI.

### 5.3 Client trades Excel — KCP's own book

- Source: shared OneDrive. Same shape as flow Excel but is the desk's actual executed trades for clients.
- Treated as a **separate stream** in the analysis, not merged with flow. Rationale: flow is "what paper is doing"; client trades are "what we did for our clients." They answer different questions and should not be cross-contaminated.

### 5.4 Economic events

**Primary: FMP free-tier economic calendar API.**

- Endpoint: `GET https://financialmodelingprep.com/stable/economic-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&apikey=...`
- One pull per weekly run. Historical events cached locally in `data/events/YYYY-Www.json` so we never re-pull.
- Fields per event: `date` (UTC ISO), `country`, `event`, `previous`, `estimate`, `actual`, `change`, `impact`.
- Filter to `country == "US"` and tier-1 / tier-2 events via regex matchers (§7).

**Fallback: Bloomberg ECO CSV export.**

- If FMP returns empty / errors / has gaps on tier-1 events, desk drops a weekly CSV export from the Bloomberg ECO screen at `data/events/bloomberg_eco_YYYY-Www.csv`.
- Loader prefers Bloomberg CSV if present for the week, else falls back to FMP.
- Loader abstraction: `events_api.py::load_events_for_week(week)` is the only function the rest of the system calls. Source is pluggable.

**Fallback of fallbacks:** if neither is available, the rundown header prints "Events data unavailable for this week — analysis is OI+flow only" and the week collapses into a single flat segment.

### 5.5 FOMC statement — option C scraper

When a FOMC meeting falls in the week:

- Primary: scrape `federalreserve.gov/newsevents/pressreleases/monetaryYYYYMMDDa.htm` for the statement text. Cache at `data/fomc_statements/YYYY-MM-DD.txt`.
- Diff against the previous statement (token-level + phrase-level).
- One small `claude -p` call summarises tone in 2-3 sentences ("slightly hawkish vs previous: removed 'additional firming', retained 'data-dependent'"). This is the *only* place the LLM touches FOMC classification.
- If scraper fails: rundown flags "FOMC Wed, statement scrape failed — check Bloomberg/official" and the meeting is still a segment boundary but has no tone label.

## 6. Event tiers

Tier 1 (segment boundary — pre / event-day / post):

- FOMC
- CPI (headline + Core)
- NFP (Non-Farm Payrolls)
- PCE (headline + Core)
- GDP
- ISM PMI (Manufacturing + Services)

Tier 2 (mentioned as context in the rundown, not a segment boundary):

- ADP
- PPI
- Retail Sales
- Jobless Claims
- JOLTS
- S&P Global PMIs

Everything else ignored by the segmenter.

## 7. Event matching

Event names from the API are unstable across periods. Loader uses regex matchers, not exact string equality. Unmatched events go to a `misc` bucket (ignored for segmentation, surfaced in the rundown appendix if the desk wants context).

```python
EVENT_MATCHERS = {
    "FOMC":         r"FOMC|Federal Funds|Fed Interest Rate|Rate Decision.*US",
    "CPI":          r"Consumer Price Index|^CPI(?!.*Core)",
    "CORE_CPI":     r"Core CPI|Core Consumer Price",
    "NFP":          r"Non.?Farm.*Payroll|Nonfarm.*Payroll",
    "PCE":          r"PCE(?!.*Core)|Personal Consumption",
    "CORE_PCE":     r"Core PCE",
    "GDP":          r"^GDP|Gross Domestic Product",
    "ISM_MFG":      r"ISM Manufacturing",
    "ISM_SVC":      r"ISM (Non-Manufacturing|Services)",
    # tier 2
    "ADP":          r"ADP.*Employment",
    "PPI":          r"Producer Price Index|^PPI",
    "RETAIL_SALES": r"Retail Sales",
    "JOBLESS":      r"Initial Jobless|Continuing Jobless",
    "JOLTS":        r"JOLTS|Job Openings",
    "SP_PMI_MFG":   r"S&P.*PMI.*Manufacturing",
    "SP_PMI_SVC":   r"S&P.*PMI.*(Services|Composite)",
}
```

FOMC emits multiple API rows per meeting day (statement, rate decision, SEP on quarterly meetings, press conference). Dedupe: any FOMC-matched rows within a 6-hour window collapse to a single segment-boundary event. Individual rows still surface in the rundown context.

## 8. Surprise classification

Deterministic in Python. The LLM never decides hot/cold/inline.

| Event | Inline band: `|actual − estimate| ≤` |
|---|---|
| NFP | 5k |
| ADP | 10k |
| Jobless Claims | 5k |
| CPI | 0.1pp |
| Core CPI | 0.1pp |
| PCE | 0.1pp |
| Core PCE | 0.1pp |
| PPI | 0.1pp |
| Retail Sales | 0.1pp |
| GDP | 0.1pp |
| ISM PMI (Mfg + Svc) | 0.3pt |
| S&P PMIs | 0.3pt |
| JOLTS | 100k |
| FOMC | LLM-classified from statement diff (option C) |

Classifier output: `hot` (actual > estimate + band, price-bearish), `cold` (actual < estimate − band, price-bullish), `inline` (within band). Stored on the event object as `surprise`.

"Hot" on NFP and most growth data is **bearish for rates** (price-down). "Hot" on CPI / PCE / PPI is also bearish for rates. The classifier label is direction-agnostic numerically; direction wrt price is inferred in the rundown by the LLM, which is told the mapping in its system prompt.

## 9. Segmentation

For each tier-1 event in the week, the pre-aggregator produces three segments (one can be empty if the event is on Monday or Friday):

- **`pre_<event>`**: prior trading day's EOD state (1 day of OI data).
- **`event_day_<event>`**: event-date EOD state (1 day of OI data).
- **`post_<event>`**: next trading day's EOD state (1 day of OI data).

Segmentation is on **OI snapshots at daily settle**, matching the cadence of CME data. No intraday reasoning; the data doesn't support it.

A week with no tier-1 events is a single flat segment `week_flat`.

A week with two tier-1 events close together (e.g. FOMC Wed + NFP Fri) has overlapping or adjacent segments; pre-aggregator handles this by reusing the same EOD snapshots across segments rather than trying to disambiguate.

### 9.1 Worked example: CPI Wednesday

Week of 17–21 Nov 2026, CPI prints Wed 18 Nov 8:30 ET (tier-1):

| Segment | Trading days included | What it captures |
|---|---|---|
| `pre_CPI` | Mon 17 + Tue 18 EOD | Positioning *into* the event |
| `event_day_CPI` | Wed 18 EOD | Reaction at the close (data prints 8:30 ET, futures move all day, this snapshot is the end-of-day result) |
| `post_CPI` | Thu 19 + Fri 20 EOD | Follow-through or fade |

The aggregator computes per-strike ΔOI, top volume, and futures move for *each segment independently*. The LLM then sees the three segments side-by-side and can tell the rotation story:

> "Paper went into CPI long Z6 dovish calls (pre_CPI: Z6 96.75c +8.4k). Hot print on Wed (3.2 vs 3.0). They covered into the close (event_day_CPI: Z6 96.75c -7.1k) and rotated to puts (event_day_CPI: Z6 96.50p +6.2k). Build continued through Friday (post_CPI: Z6 96.50p +4.8k)."

**Two events close together**: e.g. CPI Wed + NFP Fri in the same week. Both segment around their own event; Thu's EOD snapshot is both `post_CPI` *and* `pre_NFP`. The aggregator emits it twice, once for each segment context. Same EOD numbers, different framing.

## 10. Pre-aggregator output — the structured digest

This is what the LLM actually sees. Shape:

```json
{
  "week": "2026-W47",
  "products": ["SR3", "0Q"],
  "expiries_in_scope": ["H6", "M6", "U6", "Z6", "H7", "M7", "U7", "Z7"],

  "events": [
    {
      "date": "2026-11-18",
      "matcher": "CPI",
      "event_name": "Consumer Price Index YoY",
      "previous": 2.7,
      "estimate": 2.9,
      "actual": 3.2,
      "surprise": "hot",
      "impact": "High"
    }
  ],

  "segments": [
    {
      "window": "pre_CPI",
      "trading_days": ["2026-11-16", "2026-11-17"],
      "per_expiry": {
        "SR3_Z6": {
          "top_oi_changes": [
            {"strike": 96.75, "type": "call", "delta_oi":  8400, "at_close": 52300},
            {"strike": 96.87, "type": "call", "delta_oi":  6100, "at_close": 41200},
            {"strike": 96.50, "type": "put",  "delta_oi": -2200, "at_close": 38100}
          ],
          "top_volume": [
            {"strike": 96.75, "type": "call", "volume": 12400}
          ],
          "oi_concentration": {
            "top_5_strikes_share_of_total_oi": 0.43
          },
          "futures_settle": 96.39,
          "futures_move_bp": 2.5
        }
      },
      "flow_notes": [
        "SFRZ6 96.75/96.87 cs ppr bought 5k at 2",
        "Z6 call skew offered"
      ],
      "client_trades": [
        "KCP bought SFRZ6 96.75 c 2k for client at 3"
      ]
    }
  ],

  "week_summary": {
    "top_oi_builds_overall":    [/* ... */],
    "top_oi_unwinds_overall":   [/* ... */],
    "flow_vs_oi_correlations":  [/* ... */]
  },

  "prior_weeks": [
    {"week": "2026-W46", "headline_bullets": ["...", "..."]},
    {"week": "2026-W45", "headline_bullets": ["...", "..."]}
  ]
}
```

Size target: under ~15KB of JSON. Never send the LLM raw per-strike rows. Keep the digest narrow and structured.

### 10.1 Aggregation rules

Per (expiry, call_or_put):

- **`top_oi_changes`**: top 5 strikes by `|ΔOI|` over the segment's trading days. Signed; a negative delta means OI was unwound.
- **`top_volume`**: top 5 strikes by total volume over the segment's days.
- **`oi_concentration`**: share of OI held by the top-5 strikes vs all strikes, as a concentration proxy.

Per expiry, the **futures settles** are pulled from the CME file's top `Futures` section (§5.1) for the **underlying quarterly** of that expiry. A V6 option expiry's `futures_settle` is the Z6 future's settle (resolved via `products.underlying_quarterly()`), since V6 options settle into Z6 futures.

- **`futures_settle`**: closing settle of the underlying quarterly future at the end of the segment.
- **`futures_move_bp`**: price change of that future across the segment, in bp (positive = rates lower).

**Why we capture futures settles:**

1. **Strike-level context.** A `+8.4k ΔOI` on a 96.75 call means different things if the future is at 96.39 (paper positioning above-screen for further rally) vs at 96.85 (paper rolling existing ATM positions). The settle frames every strike-level observation.
2. **Event reaction validation.** The events API tells us CPI printed hot. The futures move tells us if the market actually reacted. CPI hot + future barely moved = market shrugged; CPI hot + future -15bp = real reaction. The LLM uses this to write honest narratives.

Cross-expiry and cross-stream:

- **`flow_notes`**: all flow rows whose `date` falls inside the segment's trading days. Keep the raw note; the LLM parses meaning.
- **`client_trades`**: all client-trade rows whose `date` falls inside the segment. Kept separate from flow_notes in the digest.

## 11. Cross-week memory

Two sidecar inputs to the LLM:

1. **`data/weekly_digests/YYYY-Www.json`** — the structured digest from the past 3 weeks. LLM can compare ΔOI across weeks: "Z6 97 calls +45k this week vs +18k last week vs +22k prior — build-up accelerating."
2. **`data/weekly_digests/YYYY-Www_headlines.md`** — 3-5 bullet points extracted from each prior rundown's headlines section. LLM sees the framing of the prior 2 weeks' conclusions so it can reference continuity ("the dovish Z6 positioning built over the last 2 weeks was unwound into Wed's hot CPI").

The LLM sees: this week's full digest + last 3 weeks' structured digests + last 2 weeks' headline bullets.

Window chosen narrow on purpose. Deeper history = context bloat = worse output.

## 12. LLM prompt contract

Single `claude -p` call per weekly run. System prompt lives at `src/kcp_structgen/prompts/weekly_analysis.md`.

The system prompt covers:

- **Role**: "You are a desk analyst writing the weekly rates-options positioning note for a STIR-focused options sales desk."
- **Input**: the structured digest described in §10, plus prior-weeks context.
- **Output contract**: markdown. Fixed section order — `## This week's headlines` (3-5 bullets), `## Events` (per tier-1 event: what printed, how paper positioned into it, how they adjusted after), `## OI themes` (build-ups, unwinds, strike clustering), `## Flow highlights` (paper colour that correlates with OI moves), `## KCP client activity` (our client book, separately), `## Watch for next week` (2-3 forward bullets).
- **Constraints**:
  - Every numerical claim must cite the specific strike / ΔOI / date from the digest. No invented numbers.
  - If the digest has no data for a section (e.g. quiet flow week), say so explicitly — do not pad with generic prose.
  - No trade recommendations. Describe positioning, not action.
  - **No fences.** Raw markdown. First and last characters are heading hash and newline.
- **"How to think" examples** (few-shot in the prompt): 3-4 fully-worked example digests paired with expected rundown output. Examples show: (a) event-segmented positioning story, (b) flow-OI divergence, (c) quiet week honest output, (d) cross-week continuation theme.

## 13. Rundown format (v1: option C — show raw evidence)

Every observation cites its evidence inline. No "confidence: high/medium/low" labels. Example:

```markdown
## This week's headlines
- Paper rotated out of Z6 dovish calls into puts around Wed's hot CPI print
  (Z6 96.75 c ΔOI -7.1k Wed, Z6 96.50 p ΔOI +6.2k Wed; flow notes: "Z6 ppr
  covered calls, bought 96.50 puts")
- Upside build in 0Q H7 96+ accelerated for a third week (+14k cumulative
  across 96.00/96.12/96.25 calls; Mon flow: "0Q H7 upside bid")
```

Scoring-and-tiering layer (option D) is deferred — v2_backlog.md tracks it. After 2-3 real runs we review the raw output and decide what's worth scoring.

## 14. App integration

New tab in the existing Tkinter app. Tab label: **Weekly Analysis**. Layout:

- **Top section**: buttons — `Import OI file (today)`, `Load flow sheet`, `Load client-trades sheet`, `Generate weekly rundown`.
- **Middle section**: read-only status panel showing: how many daily OI files loaded for this week, how many flow rows, how many client-trade rows, how many events pulled, FOMC statement status (scraped / cached / failed).
- **Bottom section**: markdown preview pane + `Copy to clipboard` button.

The existing Structure Generator tab is untouched. Code is cleanly split: new module `src/kcp_structgen/analysis/` contains the Layer 2 logic; `gui.py` gains the tab but doesn't otherwise change.

## 15. Module layout

```
src/kcp_structgen/analysis/
    __init__.py
    cme_loader.py        # parse VoI .xls -> structured daily digest JSON
    flow_loader.py       # parse flow + client-trades Excel sheets
    events_api.py        # FMP primary, Bloomberg CSV fallback; unified output
    fomc_scraper.py      # federalreserve.gov scrape, statement diff, tone call
    event_matcher.py     # regex matchers, tier classification, FOMC dedupe
    classifier.py        # surprise bands (§8) -> hot/cold/inline labels
    segmenter.py         # week -> list[Segment] using tier-1 events
    aggregator.py        # segments + OI digests + flow + client -> structured digest
    memory.py            # load prior-weeks digests + headlines
    runner.py            # orchestration: inputs -> digest -> claude -p -> markdown
    prompts/
        weekly_analysis.md
```

Tests mirror this layout under `tests/analysis/`.

## 16. Storage layout

```
data/
    oi/
        daily/
            2026-11-16.xls        # raw CME file (desk-dropped)
            2026-11-16.json       # parsed digest (code-written)
            ...
    flow/
        flow.xlsx                 # live, OneDrive-synced
    client_trades/
        client_trades.xlsx        # live, OneDrive-synced
    events/
        2026-W47.json             # FMP cache for this week
        bloomberg_eco_2026-W47.csv  # optional fallback
    fomc_statements/
        2026-11-18.txt            # scraped and cached
    weekly_digests/
        2026-W47.json             # structured digest
        2026-W47_headlines.md     # extracted headlines
        2026-W47_rundown.md       # full rundown for reference
```

`data/` is in `.gitignore` (real OI / flow is not committed). A small `data/examples/` subfolder with synthetic files for tests is committed.

## 17. Failure modes and fallbacks

| Condition | Behaviour |
|---|---|
| CME file missing for 1-2 days of the week | Warn in rundown header. Aggregate over the days we have. |
| CME file missing for ≥3 days of the week | Refuse to generate. "Insufficient OI data." |
| Flow sheet empty | Proceed with OI-only analysis. Rundown `Flow highlights` section says "No flow logged." |
| Client-trades sheet empty | Proceed. `KCP client activity` section says "No client trades logged." |
| FMP returns empty / errors | Try Bloomberg CSV fallback. If absent, flat-segment week with warning in header. |
| FOMC scraper fails on a FOMC week | Flag in rundown; segment boundaries still present; no tone summary. |
| `claude -p` times out (120s) | Fail loud. Error panel in GUI. No silent retry. |
| `claude -p` returns non-markdown (e.g. fenced) | Parser strips fences defensively before showing in GUI — same belt-and-braces as Layer 1. |

## 18. Hard constraints (inherited from Layer 1)

Same as the structure generator:

- **No per-call API charges.** `claude -p` subprocess only.
- **No browser automation.** Clipboard paste via `pyperclip`.
- **No local options math.** Deterministic Python owns all the aggregation; LLM owns the narrative.
- **Windows + Chrome + Claude Code** is the runtime target.
- **Non-engineer users.** Desk members click buttons; no CLI.

## 19. Testing strategy

- **Unit tests per module** — cme_loader parsing, event matchers, classifier bands, segmenter, aggregator. Synthetic fixture files at `tests/analysis/fixtures/`.
- **Golden rundown tests** — a handful of hand-labelled weeks (synthetic OI + synthetic flow + synthetic events) with expected structured-digest output. These test the deterministic pipeline, not the LLM.
- **LLM smoke test** — one end-to-end test that runs `claude -p` against a fixture digest and asserts the output is valid markdown with the expected section headers present. Skipped if `claude` is not on PATH, like Layer 1 golden tests.

## 20. Definition of done for v2

All of the following green:

1. Desk drops 5 daily CME files for one week + fills out flow sheet + fills out client-trades sheet.
2. `Generate weekly rundown` produces a markdown note covering the full week.
3. Note is copied to clipboard and shared with the desk.
4. At least 2 consecutive weekly runs are reviewed, and the user confirms output is desk-readable and accurate (not hallucinated).
5. v2_backlog.md is updated with what we've learned about scoring / tiering / other gaps from the real runs.

## 21. Open questions to close before build

None at time of drafting. All earlier decisions (cadence, scope, data sources, segmentation, tier lists, event matching, classification bands, FOMC option C, confidence option C) are settled per conversation 2026-04-24.

If this spec is frozen without additions, build order is: `cme_loader.py` → `flow_loader.py` → `events_api.py` → `classifier.py` + `event_matcher.py` → `segmenter.py` → `aggregator.py` → `fomc_scraper.py` → `memory.py` → `prompts/weekly_analysis.md` → `runner.py` → GUI tab.

## 22. Charts (added 2026-04-24)

`claude -p` cannot produce binary artefacts (PNG/SVG). Charts are produced by the same responsibility split used everywhere else in this project: **LLM picks which charts matter; Python renders them deterministically.**

### 22.1 Mechanism

The LLM may include 0-N chart spec blocks inside its markdown rundown. Each spec is a fenced JSON block tagged `chart`:

```chart
{
  "type": "bar",
  "title": "Top OI builds — week of 2026-W47",
  "x": ["96.50p", "96.75c", "96.87c", "97.00p"],
  "y": [-2200, 8400, 6100, -3800],
  "y_label": "ΔOI",
  "highlight_color_rule": "positive=green,negative=red"
}
```

A Python post-processor (`analysis/charts.py`) walks the rundown, parses each chart block, calls matplotlib to render to `data/weekly_digests/2026-W47_chart_N.png`, and replaces the chart block with a markdown image link `![title](2026-W47_chart_N.png)`.

### 22.2 Allowed chart types (enum)

The LLM is told in its system prompt that it may only emit charts of these types:

- `bar` — one categorical axis, one numerical axis. Used for "top N strikes by ΔOI" and similar.
- `line` — two numerical axes. Used for "OI build over weeks" and similar time-series.
- `stacked_bar` — one categorical axis, multiple stacked numerical series. Used for "calls vs puts ΔOI per expiry".
- `heatmap` — two categorical axes, numerical cell values. Used for "strike × expiry concentration".

No other types. No free-form Plotly-style configs. If the LLM emits an unknown `type`, the post-processor warns and skips that chart.

### 22.3 Style

Renderer applies one consistent style across all charts: dark background to match the GUI palette (BG `#0e1117`, panel `#161b22`), accent green for positive series, red for negative, neutral grey gridlines. Style lives in one constant in `charts.py` — no per-chart customisation.

### 22.4 Failure modes

| Condition | Behaviour |
|---|---|
| LLM emits `type` not in enum | Skip that chart; warn in rundown header. |
| Chart spec missing required fields | Skip that chart; warn. |
| matplotlib render fails | Skip; warn; rest of rundown still renders. |
| User wants chart-free output | Renderer takes a `--no-charts` flag that strips all chart blocks. |

### 22.5 Pinning the chart vocabulary

When the user describes which visualisations are most useful for the desk, the chart-type enum (§22.2) gets locked. Any new type added later goes through a spec amendment. This keeps the post-processor predictable and the LLM's chart choices bounded.

### 22.6 Testing

- Unit tests per chart type with synthetic specs. Compare rendered PNG byte-hash for regression. Any change to rendering style requires regenerating fixtures.
- Spec validator: assert any markdown the LLM produces has either zero chart blocks or only blocks with valid `type` and required fields.
- End-to-end smoke test: synthetic digest → `claude -p` → at least one chart block produced and rendered without error. Skipped if `claude` CLI not on PATH (same gating as Layer 1 golden tests).
