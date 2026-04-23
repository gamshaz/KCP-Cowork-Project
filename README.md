# KCP STIR Structure Generator

Desktop tool for the KCP rates sales desk. Type a trade scenario in natural language; get a grouped list of listed STIR option structures formatted in PricingMonkey trade-description grammar. Copy, paste into PM, PM prices and validates.

Products v1: SOFR, Euribor, SONIA — whites and reds mid-curves. Listed options + futures only.

See [CLAUDE.md](CLAUDE.md) for architecture, [docs/spec.md](docs/spec.md) for the frozen v1 spec, [docs/plan.md](docs/plan.md) for the build order.

## Install (dev)

```
pip install -e .[dev]
```

Requires Python 3.11+ and a working `claude` CLI (Claude Code under your Enterprise seat).

## Run

```
python -m kcp_structgen
```

## Test

From the project root, after `pip install -e .[dev]`:

```
pytest
```

- Enumerator, strike, and product tests run without Claude Code.
- Golden parser tests (`tests/golden/*.json`) auto-skip if the `claude` CLI isn't on PATH. On a desk PC with Claude Code installed they run automatically.
- To run just one file: `pytest tests/test_enumerator.py`
- To see verbose output: `pytest -v`

## Prototype behaviours

These are **deliberate for the prototype phase** and will be removed once a Bloomberg (`blpapi`) feed is wired in:

- **The tool asks for the current futures price on every scenario** that didn't include an explicit anchor number. `current_rates.json` only holds the cash rate — the futures price differs whenever the curve prices in an expected move, and it's the futures price that decides call-vs-put direction.
- **The dialog asks for the underlying quarterly, not the option monthly.** ERQ6 options settle into ERU6 futures, so the prompt is "What is the current **ERU6** futures price?" — type the U6 (or X6, M6, H6) futures screen price.
- **30s timeout bumped to 120s.** The first `claude -p` call after login can take 30–60s. If it times out, run `claude` once in a terminal to warm the session.

## Known editor warning (ignore)

Pylance may show `Import "pytest" could not be resolved` if VS Code picked the wrong Python interpreter. `Ctrl+Shift+P → Python: Select Interpreter` → pick your system Python 3.13, then `Developer: Reload Window`. Runtime is unaffected either way.

