# RKO interface direction

## Product context

RKO is a local research and paper-simulation dashboard. It keeps four clearly separated views: the V5 inspected-history comparison, an opt-in forward USD paper cohort, V4 Binance USDT historical research, and the retired read-only legacy USD snapshot with a synthetic demo. The forward cohort has optional per-position TP/SL settings, OFF by default; these are simulated fresh-quote requests, not guaranteed price protection. The browser uses vanilla JavaScript; the local service and storage use Python's standard library and SQLite.

The interface must make losses, uncertainty, inspected history, source units, session limits, and simulated execution easy to recognize. It must not suggest that a candidate is a winner or that modeled fills are executable returns. V4/V5 Binance USDT research is not connected to the CoinGecko USD paper cohort.

## Visual tokens

| Role | Value |
| --- | --- |
| Reading background | `#f5f4ef` |
| Primary surface | `#fffefa` |
| Secondary surface | `#eeefea` |
| Main ink | `#202b28` |
| Supporting text | `#42544e` |
| Quiet text | `#586b64` |
| Action / selected state | `#356e68` |
| Positive value | `#286248` |
| Negative value | `#9a3e48` |
| Caution text | `#55451e` |
| Separator | `#d5dcd6` |
| Body / metadata | system sans, 16px / 13–14px |
| Numeric data | system sans with tabular numerals |

Use the teal for primary actions, active navigation, and the model series. Other series use distinct dash patterns and matching legend samples. Keep the navigation, table labels, controls, data provenance, and footer readable on the light surface. Controls and summary links have at least 44px hit areas and a visible keyboard focus ring. Respect reduced-motion preferences.

## Layout decisions

- Preserve the existing section structure and control behavior; strengthen hierarchy with spacing, readable type, warm neutral surfaces, and a restrained teal action color.
- Keep all desktop comparison metrics in the table. At narrow widths, replace the main V5 and forward account tables with rows that show return, drawdown, and exposure first; expandable details retain the other fields.
- Keep dense V4, paired-comparison, and audit tables in labeled internal scroll regions.
- Keep charts tied to their saved data, units, and dates. Use solid, dashed, and dotted lines with a visible legend; do not encode identity by color alone.
- Keep the session timer, absolute UTC deadline, next daily fill window, expiry policy, and fresh-quote exit state near the forward controls.
- Keep optional TP/SL thresholds in the explicit start review, explain the net remaining-basis and modeled-cost definition, and show trigger decisions and episode realized P&L beside the corresponding position history. Label quote timing, pending closure, and synthetic QA fixtures clearly.
- Use explicit words and signs for positive and negative results. No profit badge, default strategy winner, decorative gradient, glow, or glass surface.

