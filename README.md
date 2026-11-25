# Neextdrop Bot Lab

Paper-trading research on crypto strategies, built on top of [Freqtrade](https://www.freqtrade.io).
Shared in public to get feedback from people who know more than I do.

**Everything here is paper money. This is research, not investment advice. Please do not copy these trades.**

## What is in this repo

| Path | What it is |
|---|---|
| `build_results.py` | Reads Freqtrade trade databases and writes a static, read-only results page. Standard library only. |
| `backtest_data.json` | Curated 3-year backtest research, rendered by the script into `backtest.html`. Numbers only, no strategy code. |
| `results_config.example.json` | Template settings: which bots to show, start balances, notes. |
| `publish.sh` | Nightly job for the server: builds the page, scans for secrets, pushes to `docs/`. |
| `docs/` | The published results page (served by GitHub Pages). Written by the script, do not edit by hand. |
| `SERVER_SETUP.md` | Step by step setup for the server that runs the bots. |

## What the results page shows

For every bot, including the ones that failed: trade count, win rate, average result per trade with a 95% range,
profit factor, maximum drawdown, an account-value chart, monthly results, and a CSV of every closed trade.

A bot with fewer than 50 closed trades is labeled "too early to judge". A bot whose 95% range includes zero is
labeled "inconclusive". The page only calls a result positive when the statistics support it.

## Honest limits

- Paper fills ignore slippage, latency and liquidity limits. Live results would be worse.
- Rules were changed several times during testing. Bots count trades only from their current rules where a start date is shown.
- A few months of paper trading is a small sample. Feedback on method matters more than the numbers.

## Feedback wanted

If you spot overfitting, look-ahead bias, unrealistic fills, or a flaw in how results are measured,
please open an issue. Specific criticism is the most useful kind.

## Credit and license

Built on Freqtrade (GPL-3.0). Strategies, filters and the testing method are my own work.
This repository is licensed under GPL-3.0, see `LICENSE`.
