# RKO V5.3 desktop candidate validation status

**Desktop is unverified.** No current Windows package build or native Windows diagnostic has run. The Linux GTK/WebKit desktop attempts did not load an embedded page under host I/O pressure, so they do not establish a product or platform root cause. The earlier Linux binary is an internal prototype and is not a release candidate.

Test history must be read with its dates and scope: an earlier V5.2 source snapshot passed 185 tests, but that full result is stale after desktop runtime changes. A later focused desktop set passed 28 checks before the Windows notice/workflow edits. After the CSV-export portability fix, one targeted regression passed with `os.fchmod` unavailable. Static checks include Python syntax compilation, workflow YAML parsing/manual-only checks, and verification that all 16 packager-listed license files exist. The current full regression suite remains pending. These checks do not validate a native window or frozen package.

The new workflow is manual-only and targets a standard Windows 2022 x64 runner with Python 3.12. If run successfully, its offline diagnostic must prove an embedded page loaded with the session stopped, zero fills, and zero provider calls. It uploads only a sanitized summary and path-free manifest. No CI run, binary, user database, market data, saved strategy result, or screenshot is included in this candidate packet.

The app remains local and paper-only: it has no exchange-order path, credentials, or leverage. Optional per-position TP/SL is not a guaranteed stop or maximum-loss limit; sampling gaps, service downtime, modeled costs, and later fills can exceed the selected threshold. The separate 10% rule is an account-level sampled drawdown halt.
