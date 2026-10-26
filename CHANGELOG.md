# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

## [1.0.0] - 2026-10-26

### Added
- Engine runs every 15 minutes on a dedicated server; live prices (`prices.json`) on every run.
- Light incremental publishing between daily full publishes (kilobytes per refresh instead of the full package).
- Pull-based continuous deployment gated on green GitHub checks and an on-server test run, with rollback.
- Restricted operations access for the private operations repository.
- Bee's Shortlist: read-only public copy (whitelisted fields only).
- Leading setups: daily head-and-shoulders tops, bottom-accumulation breakouts with a 14-day cluster gauge.
- Parallel channels on daily and 4-hour charts with track records, chart overlay and Telegram alerts.
- Futures data: full funding history since 2019, open interest and long/short ratios.
- Market maker for MEXC spot (grid mode with buy-first rungs).
- Spot execution engine with testnet, shadow and live modes and hard risk limits.
- Test suite (unit and end-to-end scenarios), CI on Python 3.9 and 3.12, CodeQL, gitleaks, private-file guard.

### Fixed
- Funding history download stored only recent settlements; now pages from the first settlement.
- 4-hour and derivatives downloads stall on network outages; consecutive-failure circuit breakers.
