# NOTES.md — session lessons and quirks

Read this at the start of every session. One entry per lesson; update in place
rather than duplicating.

## Environment
- Local Python is 3.11.7 with SQLite 3.41.2 — `RETURNING` is available (needs
  ≥3.35), so `claim_pending_cell` is a single atomic UPDATE...RETURNING. If
  this ever runs on an older SQLite, that function needs a select-then-update
  fallback.
- venv at `.venv/`; all requirements installed cleanly on macOS (shapely and
  pdf2image wheels fine; poppler itself NOT yet installed — needed at M4, not
  before).

## SQLite / db.py
- WAL mode creates `tracker.db-wal` / `tracker.db-shm` sidecar files — added
  them to .gitignore alongside tracker.db.
- `PRAGMA journal_mode=WAL` returns lowercase `"wal"` — test asserts lowercase.
- Upsert is implemented as SELECT-then-INSERT/UPDATE inside one transaction
  (not ON CONFLICT) because the dupe path must read + merge `found_by_json`
  in Python anyway. Null-preservation via `SET col = COALESCE(?, col)`.
- Connection factory resolves db path: explicit arg > `TRACKER_DB_PATH` env
  var > `./tracker.db`. Tests use tmp_path; stage code should use the default.

## Places API (New) quirks
- **searchText rejects `circle` in `locationRestriction`.** Exact response
  (HTTP 400), captured live 2026-07-07:
  ```json
  {
    "error": {
      "code": 400,
      "message": "Invalid JSON payload received. Unknown name \"circle\" at 'location_restriction': Cannot find field.",
      "status": "INVALID_ARGUMENT",
      "details": [
        {
          "@type": "type.googleapis.com/google.rpc.BadRequest",
          "fieldViolations": [
            {
              "field": "location_restriction",
              "description": "Invalid JSON payload received. Unknown name \"circle\" at 'location_restriction': Cannot find field."
            }
          ]
        }
      ]
    }
  }
  ```
  Circle-restriction only exists on Nearby Search; Text Search allows circle
  only in `locationBias` (soft, can leak far-away results). places.py sends
  the circle's bounding RECTANGLE as the hard restriction and Stage 1's
  shapely circle check trims the corners. **Approved by Kenneth 2026-07-07**;
  SPEC.md Work order 2 amended to match.
- Pagination confirmed live: 20 results/page, `nextPageToken` on pages 1–2,
  page 3 returns no token at the 60 cap. The fresh-token INVALID_REQUEST
  retry (sleep 2s, retry once) is implemented but wasn't triggered in testing.
- `python-dotenv`'s `find_dotenv()` asserts on frame inspection when run via
  stdin/`python -` — pass the path explicitly (`load_dotenv(".env")`) in
  scripts; CLI entrypoints are fine.
- **Text Search results are nondeterministic run-to-run.** Same query minutes
  apart returns slightly different sets (940 vs 954 raw across 2 cells); with
  the 60-cap truncation, ~1-10 marginal venues flip in/out per run. So M2's
  literal "second run new_rows == 0" is unachievable live: observed 247 new →
  10 → 1 → 1 across four runs, a different marginal venue each time. The real
  invariant (zero duplicate place_ids, no data loss, dupes recognized) holds
  exactly. Treat small nonzero new_rows on re-runs as discovery, not a bug.
- Both cocktail_bar cells (river_north, west_loop) hit the 60-cap on at least
  one query → subdivision fires in dense zones; expect it across most tier-1
  cells in the full run.

## Stage 2 / identity matching
- Token overlap needs a naive plural fold (`cocktails` → `cocktail`), or real
  titles like "Machine: Engineered Dining & Cocktails" under-score vs venue
  name "Machine Cocktail Bar". Implemented in `_tokens`; 1-char strip, len>3.
- Domain matching must be substring-on-squashed-host (theviolethour.com
  contains "violet"+"hour"), not token equality — domains concatenate words.
- **25-vs-50 review floor: RESOLVED 2026-07-08 by deletion.** The whole
  rating/review gate was removed from Stage 2 (see "Stage 2 rating gate
  removed" below), so the CLAUDE.md-25 vs SPEC-50 discrepancy is moot —
  neither threshold exists anymore.

## Website fetching / bot detection (2026-07-07 incident: 12 false kills)
- **Restaurant hosting platforms block the Python TLS stack outright.**
  All 12 first-round website_dead kills were false (verified externally by
  Kenneth); 10 clustered on Popmenu and Owner.com. 11/12 cached responses
  were HTTP 403 Cloudflare challenge pages ("Just a moment..."); 1 (Bodega
  Bar, theberkshireroom.com/bodega) was a genuine 404.
- **Headers don't fix it — it's TLS (JA3) fingerprinting.** Evidence: curl
  with byte-identical browser headers gets 200 where httpx gets 403 on both
  HTTP/1.1 and HTTP/2, with and without browser-like cipher ordering. No
  header set can pass; needs a real browser fetch (Playwright, M4) or a
  curl-subprocess fallback (proposed, not implemented).
- Mitigations now in place: full browser header set + one 3s retry on
  403/429/timeout in fetch.py (helps marginal cases, not JA3 blocks);
  403/429 responses are NEVER cached (a cached challenge must not "confirm"
  a second strike); **two-strike website_dead policy** — first failing run
  parks the row at needs_review with marker `website_dead_once` (auto
  re-checked next run), only a second consecutive failing run eliminates.
  SPEC.md WO4 amended to match.
- **RESOLVED 2026-07-08:** Stage 2 check 5 now escalates a failed httpx
  fetch ONCE to fetch.get_rendered (hardened Playwright) before striking —
  only on 403/429/timeout/5xx, never robots_disallowed or plain 4xx. The
  old "don't run stage2 twice in a row" caution no longer applies: strike
  two only lands after a rendered attempt also fails. Two-strike policy
  kept because hardened Playwright itself flakes (order.toasttab.com
  rendered for Theory, 403'd for Parlay the same day). SPEC WO4 amended
  2026-07-08; see "Stage 2 Playwright escalation" below for the drain
  results.
- Mercadito preview of identity-check pitfalls: mercaditorivernorth.com
  301s cross-domain to mercaditorestaurantgroup.com (parent group site).
  This one still passes (squashed domain contains "mercadito"), but venue
  sites that redirect to a group domain NOT containing the venue name will
  land in the 0.2–0.6 needs_review band. Expected, but expect a cluster of
  them for restaurant-group venues.

## Stage 3 / M4 lessons (2026-07-07)
- **Playwright DOES defeat the Cloudflare JA3 wall — but only hardened.**
  Plain `chromium.launch(headless=True)` still gets the "Just a moment..."
  challenge: the headless shell self-identifies via UA ("HeadlessChrome")
  and `navigator.webdriver: true`. The working combination (fetch.py
  `get_rendered`): `--disable-blink-features=AutomationControlled`, a real
  Chrome UA on the context, an init script hiding `navigator.webdriver`,
  and a settle loop that re-polls the title until the challenge resolves
  (~5-10s). Verified live on franklinroom.com: real 700KB homepage, real
  title. The 12 Stage 2 retry-queue venues are now rescuable — re-run
  stage2 or wait for the full Stage 3 run.
- Challenge pages and non-200 rendered results are never cached (same
  poisoning rule as raw 403s). Rendered fetches cache under a separate
  `playwright:` + url key so raw and rendered content coexist.
- **fetch cache was text-only and would have corrupted PDFs** — binary
  content types now cache as `content_b64`; `CachedResponse.content` gives
  bytes back. Old cache entries lack the key and load fine (default None).
- robots.txt now respected in fetch.py (fail-open on errors/non-200,
  per-host parser memoized, 200 bodies persistently cached). Disallowed
  URLs return status=-1 / `robots_disallowed` without fetching.
- Anthropic structured outputs (`output_config.format` json_schema) are the
  right tool for the strict extraction schema — schema-valid JSON
  guaranteed, nullable fields via anyOf, no output repair. Models per
  CLAUDE.md policy: `claude-haiku-4-5` text, `claude-sonnet-5` vision.
- Golden harness design: cases in tests/golden/cases/*.json hold readable
  web fixtures (binary menus generated by PIL at test time), the recorded
  LLM response, and expected end state. Tests point fetch.CACHE_DIR at a
  tmp dir built from the case and replace `_do_get` with a guard that
  fails any cache miss — zero network by construction. The same cases are
  the regression set for future prompt changes (swap recorder for real llm).
- poppler installed via brew (pdftoppm 26.07.0); playwright + chromium
  headless shell installed in .venv.
- **BLOCKER for live Stage 3: `ANTHROPIC_API_KEY` is empty in .env.** The
  full pipeline ran live except the messages.create call (auth error).
  Error handling confirmed safe: the failed venue stays at 2_filtered_ok.
- Deferred (noted for later work orders): Places Photos API fallback for
  venues with no findable menu; Batch API for the full ~177-venue run;
  LLM identity tiebreak for stage-2 needs_review rows.

## Menu-provider follow (2026-07-08, fix for the 2/10 smoke pass rate)
- Root cause of most smoke misses: menus hosted on third-party platforms
  (Toast/toasttab, BentoBox/getbento, Popmenu, Grubhub, ...). The menu never
  enters the venue page DOM even after Playwright rendering (Parlay: 2.26MB
  rendered HTML, 4.4K visible chars, zero prices) — NOT a js-shell-threshold
  problem; the hosted page itself must be fetched.
- Fix in stage3_extract: `MENU_PROVIDER_DOMAINS` (host-suffix match) +
  `provider_links()` scans <a> AND <iframe> on the homepage and every fetched
  content page; up to MAX_PROVIDER_PAGES=2 hosted pages fetched via
  fetch_page (JS-shell -> Playwright escalation applies, Toast pages are
  SPAs). Hosted pages count as content, so a hosted-menu venue is never
  menu_unavailable. Golden cases 11 (Toast JS shell -> rendered) and
  12 (BentoBox PDF -> vision) cover it.
- LLM input ordering matters: hosted-menu text is placed immediately after
  the homepage part so the 24K-char budget can never truncate the actual
  menu behind less useful site pages.
- Discovery changes (Kenneth-directed, 2026-07-08): known-path guesses are
  now used ONLY when sitemap+nav+Claude find nothing (they mostly 404 and
  waste fetches once real pages are known); candidate dedup and fetched-page
  dedup are trailing-slash- and redirect-(final_url-)insensitive. NOTE:
  CLAUDE.md Stage 3 wording still lists known paths first — flagged, not
  edited.
- stage3 CLI gained `--place-id` (repeatable) for re-running a specific set;
  still gated on stage=2_filtered_ok so eliminated rows can't be reprocessed.
- **Provider bot walls vary.** grubhub.com: raw httpx 200. ezcater.com: 403
  httpx, Playwright OK. order.toasttab.com: 403 httpx AND flaky under
  hardened Playwright (Theory's page rendered fine; Parlay's stayed 403).
  ubereats.com: 307 to a challenge page. Failures are non-fatal — the venue
  just extracts from whatever else was found.
- **Provider links can belong to a sister venue** (Parlay's site links to
  order.toasttab.com/online/joydistrict — Joy District, same group). The
  extraction prompt's menu_matches_venue check is the guard; watch for it
  in audits.
- **Cloudflare email obfuscation corrupted event_contact_email**: protected
  addresses render as "[email protected]" and Haiku faithfully copied it
  (Hawksmoor, confidence=high). Fixed deterministically: html_to_text now
  XOR-decodes data-cfemail (verified live: chicago@thehawksmoor.com), and
  sanitize_extraction() nulls any non-email-shaped value with flag
  invalid_email_nulled. Matters because email-contactability is ~15% of the
  Stage 4 score and drives top-500 ordering.

## Sonnet low-confidence retry (2026-07-08, model policy now implemented)
- CLAUDE.md model policy ("low-confidence retries: Sonnet-class") is now in
  llm.extract_venue_data: a low-confidence TEXT extraction is retried once
  on claude-sonnet-5; the retry result wins either way; vision calls (already
  Sonnet) never retry. Unit-tested with a fake client.
- **Haiku confidence flaps on identical input**: Berkshire Room went
  medium -> low -> medium across three runs of the same cached content. The
  retry is the safety net for downward flaps.
- On the 10-venue smoke, all 4 Sonnet retries CONFIRMED low — those sites
  genuinely print no pricing (City Winery, Doc B's, Starbucks Reserve,
  Lulu's). needs_review now means "verified unclear", not "Haiku shrugged".

## 10-venue smoke re-run results (2026-07-08, after all fixes)
- Was 2/10 genuine passes. Now: 5 -> 3_enriched (Parlay, Hawksmoor, Theory,
  Club Lago, Berkshire), 1 menu_unavailable flag (Kitty's), 4 needs_review
  (Sonnet-verified low). Venues with real extracted numbers: 2 -> 4-5.
- Hawksmoor's menu_identity_mismatch fixed by the discovery reordering (now
  reads /us/locations/chicago/... instead of the UK menu): high confidence,
  $69 filet, capacity 500, real email.
- Parlay + Club Lago pass at medium confidence with event evidence but no
  numbers (their sites print none; Parlay's toast page 403'd). Not wrong —
  but Stage 4's event-capability gate will judge them.
- 14 LLM calls for 10 venues (4 Sonnet retries); ~7K input tokens/venue on
  Haiku, ~4-12K on Sonnet retries. Cheap: full 164-venue run est. $3-5 sync.

## Type blocklist made config-driven + corporate-fit categories (2026-07-08)
- Blocklist moved from a hardcoded set in stage1_search.py to hand-reviewed
  config/blocked_types.json. Two lists: blocked_types (lodging/casino/
  liquor_store — match anywhere in types, unchanged) and
  blocked_primary_types (match primary_type ONLY, unconditional):
  night_club (hybrid exemption removed), cafe/coffee_shop/
  breakfast_restaurant, and activity venues (barber_shop,
  miniature_golf_course, movie_theater, sports_complex, bowling_alley,
  amusement_center, karaoke). Kenneth's call for corporate-networking fit:
  these sell per-person activity/daytime models, not the F&B-minimum/buyout
  model the pipeline extracts and scores.
- **Primary-only matching is load-bearing**: secondary tags are noisy —
  The Publican, avec, Beatrix, Gene & Georgetti all carry brunch_restaurant/
  cafe/breakfast_restaurant side tags and must survive (regression-tested).
- Stage 2 mirrors the check as its first offline gate (reason blocked_type)
  and `python -m src.stage2_filter --sweep-blocked-types [--dry-run]`
  retroactively eliminates live rows — idempotent, re-run after any config
  edit. Live sweep 2026-07-08: 15/229 eliminated (8 nightclubs incl.
  Sound Bar + Spybar which were already 3_enriched, Starbucks Reserve,
  Nimble, Chicago Waffles, Puttery, Rooftop Cinema Club, City Pool Hall,
  Blind Barber). Known collateral, hand-resurrectable if wanted: Celeste
  (misclassified cocktail lounge) and Blind Barber (cocktail lounge behind
  a barbershop front). Kept by decision: live_music_venue (City Winery,
  Bottom Lounge). Stage 3 queue 175 -> 166. Elements Nightlife's
  website_dead_once retry entry is moot (swept as blocked_type).
- SPEC WO3 step 2 + WO4 check list amended (blocked_type is now WO4 check
  1; HTTP=5, identity=6); CLAUDE.md Stage 1 bullet + repo layout updated.

## Stage 2 rating gate removed (2026-07-08, approved)
- Check 2 (rating >= 4.0 AND user_rating_count >= 50) deleted from Stage 2;
  rating_below_4 / too_few_reviews are no longer producible. Rationale:
  filter on fatal, score on quality — Stage 4's rating*log(review_count)
  weight (~20%, untouched) makes low-rated venues rank lower instead of
  dying. SPEC WO4 + CLAUDE.md Stage 2 amended (dated notes); checks
  renumbered to operational -> price -> website -> identity.
- The 43 gate-killed rows were resurrected to 1_geo_ok (never jumped to
  2_filtered_ok — they'd never had the website/identity checks) and
  re-filtered: 27 -> 2_filtered_ok, 5 no_website, 8 website_identity_
  mismatch, 2 identity needs_review (Fame Cocktail Club, Drinking &
  Writing Theater), 1 website_dead_once (Elements Nightlife: httpx timeout
  + rendered 522 — auto-rechecks next run). Zero LLM calls. Stage 3 queue
  173 -> 200.
- **The resurrected population is hotel/parent-brand heavy** (it skews
  nightclubs + hotel bars, which is what sub-4.0 ratings select for): 6 of
  the 8 identity mismatches are venues whose website lives on a parent
  domain — H Bar (hyatt.com), Fulton Tap (hilton.com), Holloways Bar +
  The Chicagoan Lobby Bar (marriott.com), Mariposa + Bar on 4
  (stores.neimanmarcus.com). Correct kills under the identity rule (the
  page doesn't identify the venue), but they're exactly the
  hotel_domains.json pattern — a human rescue pass over
  website_identity_mismatch rows would be cheap if we're short of 500.
  Also: Cava Room died because its URL redirects to sibling venue
  moescantina.com (restaurant-group redirect, the Mercadito pattern).
- M5/Stage 4 follow-up (NOT implemented): with the gate gone, scoring must
  define deterministic handling for rating = NULL and tiny review counts —
  log(1) = 0, log(0) undefined — a floor/default decided in code at M5.

## Stage 2 Playwright escalation + retry-queue drain (2026-07-08)
- Check 5 now: httpx -> on 403/429/timeout/5xx escalate ONCE to
  fetch.get_rendered -> rendered 200 feeds check 6 (identity reads the
  RENDERED html + final URL) -> rendered failure falls into the unchanged
  two-strike policy. No escalation on robots_disallowed (-1) or plain 4xx
  (404/410 are genuinely dead, not bot-walled). Happy path untouched.
  Promotion clears the website_dead_once marker (set_stage writes reason
  NULL on non-eliminated transitions — already true, now test-covered).
- Drain results (queue of 12): 9 -> 2_filtered_ok (Mercadito, L Station,
  Bassment, Bar Goa, Franklin Room, AMBAR, Federales Fulton, Tree House,
  676/Omni). 1 rescued but identity band: Moe's Cantina (score 0.50 —
  "River North" suffix tokens absent from title; marker cleared, now in the
  identity needs_review pool). 2 eliminated website_dead: Bodega Bar
  (genuine 404, correctly NOT escalated, strike 2 — expected) and Viaggio
  (see below — flagged to Kenneth as a probable false kill).
- **Cloudflare has tiers.** The plain JS challenge self-resolves in the
  hardened headless browser (franklinroom.com et al. — 10 of 10 rescued).
  The INTERACTIVE/managed challenge (`cf-mitigated: challenge` response
  header, Turnstile widget) does NOT self-resolve headlessly and also 403s
  curl-with-browser-headers now: viaggiochicago.com serves it to httpx,
  hardened Playwright, AND curl (verified 2026-07-08; curl got 200 on these
  sites on 07-07, so the site tightened its setting or is under attack
  mode). No amount of settle-loop waiting fixes a challenge that requires
  a click. If more of these appear in the full run, options are manual
  verification or a needs_review park keyed on `cf-mitigated: challenge`;
  decided nothing yet.
- 676 Restaurant & Bar (Omni hotel) is now at 2_filtered_ok — reminder
  that the hotel_domains.json proposal (below) is still unimplemented; it
  would have parked this row for human review.

## Hotel-restaurant blocklist gap (proposal only, NOT implemented)
- 676 Restaurant & Bar is the Omni Chicago Hotel's restaurant, but its
  Places types are purely restaurant/bar (`american_restaurant,
  cocktail_bar, bar, restaurant, food, ...`) — no `lodging` — so the Stage 1
  type blocklist structurally cannot catch hotel restaurants. The only
  reliable signal in our data is the website: it lives under
  omnihotels.com/hotels/chicago/dining/.
- Proposed fix (deterministic, code-decided): add
  `config/hotel_domains.json` with known hotel-brand domains (omnihotels,
  marriott, hilton, hyatt, ihg, fourseasons, langhamhotels, ...). In Stage 2
  after the fetch, if the FINAL redirect domain matches the list, set
  `needs_review` with marker `hotel_restaurant` rather than auto-kill —
  some hotel venues (rooftops, standalone-brand restaurants) are viable
  event spaces, so a human decides. Config is hand-reviewed like zones.json.
  Already-seen candidates in the data: 676/Omni, Holloways Bar (marriott
  .com), Bar Pendry (pendry.com), Upstairs at The Gwen (thegwenchicago.com).

## Decisions made beyond the spec letter (flag to Kenneth if they seem wrong)
- Dupe upsert with a NON-null incoming scalar DOES overwrite the old value
  (spec only forbids null-overwrites; fresher Places data should win).
- An exactly-identical `found_by` entry (same cell_id+query+rank+run_id) is
  NOT appended twice — keeps re-runs idempotent; distinct run_ids still append.
- `upsert_venue` never touches `stage`/`eliminated_reason` on dupes, so a
  re-run of Stage 1 can't resurrect an eliminated row back to 0_raw.
- `set_stage(..., 'eliminated')` without a reason raises ValueError — enforces
  the "every kill has a reason code" invariant at the DB layer.
- Non-mandated modules (places, fetch, llm, stage1, stage2) created as
  docstring-only stubs so the CLAUDE.md repo layout exists without starting
  Work order 2.

## Spec sync (2026-07-08, approved by Kenneth): both open decisions resolved
- CLAUDE.md model policy updated: Stage 3 runs use the synchronous Messages
  API by default; Batch API only worth revisiting if the queue grows roughly
  an order of magnitude above current volume (sync cost would then
  meaningfully exceed batch cost), since adopting it means restructuring the
  interleaved fetch/extract flow into collect-all -> submit -> poll ->
  write-back phases.
- CLAUDE.md Stage 3 discovery wording updated to match the implementation:
  sitemap.xml grep -> nav-link keyword match with Claude fallback -> known
  paths as last resort, only when the first two find nothing (verified
  against discover_candidates in stage3_extract.py before editing).
- Spec-only change; no pipeline code touched. pytest = 99 passed after the
  edits. CLAUDE.md is now synced with the implementation, and the full
  164-venue Stage 3 sync run is unblocked.

## State as of 2026-07-08
- Menu-provider follow + discovery cleanup + Sonnet low-conf retry + cfemail
  decode all implemented and live-verified on the 10-venue smoke (see the
  2026-07-08 sections above). pytest = 99 passed, golden set now 12 cases.
- Tracker after the 2026-07-08 drain + gate removal + 25-venue Stage 3 run
  + blocked-types sweep: 166 at 2_filtered_ok (Stage 3 queue), 24 at
  3_enriched, 45 eliminated (15 blocked_type, 10 no_website, 9
  website_identity_mismatch, 9 price_level_high, 2 website_dead), 24
  needs_review (14 identity band, 8 extraction_low_confidence, 2
  menu_identity_mismatch). website_dead_once queue: empty.
- Batch-vs-sync for the full 164 run: RESOLVED 2026-07-08, see the spec-sync
  entry below (sync by default; CLAUDE.md updated).
- CLAUDE.md discovery wording drift: RESOLVED 2026-07-08, see the spec-sync
  entry below (known paths documented as last resort; CLAUDE.md updated).

## State as of 2026-07-07 (late)
- M4 (Stage 3) implemented: llm.py (structured-output extraction + nav-link
  fallback), stage3_extract.py (discovery → routing → ONE extraction call →
  deterministic price rules), fetch.py (binary cache, robots.txt, hardened
  Playwright fallback), db.write_extraction. `pytest` = 85 passed including
  10 golden cases (HTML / PDF / picture / JS-shell / no-menu / price rules).
- Live Stage 3 smoke: pipeline + fetch + menu_unavailable path verified on 2
  real venues; the actual Claude call blocked on the empty ANTHROPIC_API_KEY.
  Once the key lands: `python -m src.stage3_extract --limit 5` to smoke,
  then decide Batch API for the full 177.
- Work orders 0–4 complete before this. Fixture captured from real
  M1 run (3 pages × 20 places).
- M2 done live on west_loop__cocktail_bar + river_north__cocktail_bar:
  259 venues, zero duplicate place_ids across 4 runs. new_rows on re-runs is
  1-10, not 0 — see "Text Search results are nondeterministic" above.
- M3 done live: funnel 259 → 24 rating_below_4, 19 too_few_reviews,
  9 price_level_high, 5 no_website, 1 identity_mismatch → 177 at
  2_filtered_ok, 58 eliminated, 24 needs_review (12 identity-band + 12 in
  the website retry queue after the false-kill incident above). Re-run
  processes 0 fresh rows; 200 fetches cached. M3 acceptance #3 (joint
  10-row spot-check) still owed to Kenneth.
- Deferred/open: robots.txt respect (CLAUDE.md convention) not implemented in
  fetch.py yet — single homepage GETs for now; implement before Stage 3's
  heavier crawling. LLM identity tiebreak for the 12 needs_review rows is a
  later work order. Reconcile the 25-vs-50 review floor (see Stage 2 section).
- Next: Phase 1 definition-of-done wants the M3 funnel reproducible from a
  fresh clone via README, then Phase 2 spec (Stage 3 golden set).
