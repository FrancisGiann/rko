# RKO V5.2 public test report

The sanitized source bundle and its clean ZIP extraction each passed 170 Python regression tests using generated fixtures and disposable SQLite databases. `node --check app.js`, `node --check time_utils.js`, and eight timestamp-helper assertions also passed. No market-data archives, saved strategy results, fills, or user databases were required.

The clean-extract bootstrap returned health OK, a stopped forward cohort, empty V4 data state, and no saved result using only local GETs and a new disposable database. It did not fetch price history, start a paper cohort, or evaluate a study. The synthetic-only QA launcher binds only to loopback, requires a new disposable database, and visibly labels fabricated account and position records. These checks establish interface and accounting behavior only.

No historical study was rerun for this public snapshot. It contains no saved market prices, historical strategy P&L, score ledgers, or screenshots. Synthetic regression fixtures may contain example simulated amounts. No strategy is described as profitable; there is no evidence of forward performance in this report.

TP/SL settings are simulated and optional. A trigger requires monitoring and a later fresh quote to close; provider gaps, service downtime, and estimated costs can produce a worse result than the threshold. The 10% rule is an account-level sampled drawdown halt, not an individual position stop or guaranteed maximum loss. This report makes no claim of guaranteed loss protection.
