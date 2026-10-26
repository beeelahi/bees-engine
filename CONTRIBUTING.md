# Contributing

* Branch from `main`, open a pull request, keep it focused. Checks must be green before merge; `main` deploys to
  production automatically.
* Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `perf:`,
  `docs:`, `test:`, `ci:`, `refactor:`, `chore:`).
* Standard library only in `bin/` and `tests/` (enforced by CI).
* Every new signal, setup or bot ships with its out-of-sample evidence (up to 2021 vs. 2022 on) and a research
  script in `research/`. Negative results go into `docs/RESEARCH.md`.
* Anything that touches money (execution, market making, risk limits) needs a scenario test against a simulated
  exchange, including the failure paths.
* Update `CHANGELOG.md` under "Unreleased".
