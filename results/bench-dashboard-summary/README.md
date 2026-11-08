# /summary/ — executive benchmark summary

Hand-authored HTML (`index.html`) — a one-page exec summary tying together the
seven detail pages of the benchmark report. Not generated from engine
`result-*.json` (it's narrative + roll-up tables), so it reuses the shared
`<head>`/style from a generator-produced sibling page (e.g. `glove/index.html`)
for visual consistency, like `/x86/`.

Deploy: `sudo cp index.html /data/movie-finder-public/bench-site/summary/index.html`
(served by Caddy at https://bench.myvector.online/summary/; linked as the lead
nav item on the landing page).

## Contents (roll-up of the live pages)

- Four takeaways: high-recall lead (/fair/), scaling (/glove/), neutral-harness
  reproduction (/village/), hardware-general kernels (/x86/).
- Improvements shipped: PR #278 (kernels + keepPrunedConnections), PR #280
  (ANN-rewrite overhead, +48–80% low-ef QPS), RFC-006 / PR #281 (draft).
- What we tested (engines, datasets, method, hardware) + honest caveats
  (MariaDB glove exclusion is a measured 10h non-completion; residual low-ef
  overhead; neutral-harness scope; x86 on shared CI runners).

Regenerate with `make_summary_page.py` (kept in the session scratch); update the
roll-up prose when the underlying pages change materially.
