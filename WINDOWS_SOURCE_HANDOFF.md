# RKO Windows source handoff

This is a Windows build-and-validation handoff, not a tested Windows application or a distributable binary. It is limited to the `rko-desktop-v5_3` source tree; it does not change the frozen V5.1/V5.2 publisher trees or saved historical research.

## Manual workflow

`.github/workflows/windows-desktop-manual.yml` is deliberately triggered only by `workflow_dispatch`. It uses a standard Windows 2022 x64 runner, Python 3.12, read-only repository permission, Node 24-compatible action commits, finite timeouts, and no secrets. It runs the regression suite and JS checks, creates an unsigned one-directory package, checks for the Microsoft Edge WebView2 Evergreen Runtime, and runs the app's offline read-only diagnostic with a fresh temporary data directory. It never starts a paper cohort or calls a market provider. The diagnostic must report a loaded embedded WebView, a stopped session, zero fills, and zero provider attempts.

The workflow file must first be present on the repository's default branch for GitHub to expose the manual-run control; once it is registered there, the operator can select the candidate branch as the run ref. The workflow retains only a redacted validation summary and a build manifest with local paths removed. It does not upload the executable, database, screenshot, raw diagnostic, market data, or saved research. If the diagnostic or runtime check fails, the run fails; no binary is released automatically. There is no push, tag, or release trigger.

## Dependency and license scope

Windows pins are in `requirements-windows.txt`; exact dependency notices are under `third_party/desktop` and are collected by `tools/build_windows.py`. The Microsoft WebView2 Core DLL from the pinned PyWebView 6.2.1 wheel matches the upstream tag's Git blob identity recorded in `DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md`; the matching upstream notice covers the Core, WinForms, and loader components. The Evergreen Runtime is a host prerequisite and is not bundled or installed by RKO.

The source snapshot does not select a project-wide open-source license. Third-party notices apply only to their named components. This handoff is not a distribution clearance for the RKO project or a claim that the Windows runtime has passed validation.

Windows-created data files inherit the selected directory's operating-system ACL. POSIX mode values passed to `os.chmod` are best-effort portability hints on Windows, not a Windows ACL guarantee.

## Remaining validation

No Windows runner has executed this workflow yet. The Fedora GTK/WebKit native GUI attempt did not reach an embedded page and was confounded by host I/O pressure; that result does not establish a product or platform root cause. The existing Linux binary is an earlier internal prototype and must not be redistributed. A successful Windows run is still required to validate its actual embedded renderer, navigation boundary, tokenized APIs, shutdown, and WebView2 runtime behavior. Native keyboard/window-chrome checks also remain outstanding.

The package contains no user database, market dataset, credentials, or saved study results. It does not make archived result panels available by fetching data automatically. The UI remains local, paper-only, stopped at launch, and without exchange orders or leverage.
