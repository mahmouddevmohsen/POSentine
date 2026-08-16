# POSentine — Pre-Supabase Dashboard Verification

**Date:** 2026-08-16
**Scope:** Verification only. No Supabase change, no RLS, no API, no auth, no schema change, no deploy, no commit, no push.
**Subject:** `dashboard/POSentine Arabic Dashboard/POSentine Dashboard.dc.html` (the single authoritative dashboard)

> **Method note — why this audit is adversarial.** Most of the work under review is my own Phase 1 work. This project's signature failure is *"a check that shares the fault it is meant to detect,"* so self-review was not trusted. Findings rest on measurements and **three independent agents** (adversarial semantics, security, Supabase permissions) plus live browser instrumentation and arithmetic recomputed from the file itself.
>
> That was the right call: **the independent audit found defects my own Phase 1 verification declared clean**, and a majority of the serious findings below are mistakes **I introduced**. They are marked ⚠️**self-inflicted**. One agent claim was itself wrong and is corrected rather than propagated (see §4, note on the owner's name).

---

## 1. Executive Verdict

# 🔴 NOT READY

This is not a judgement on the design or the direction, both of which are sound. It rests on three specific facts:

1. **Six self-contradictions are visible on screen today.** The dashboard states two different totals for the same period, opposite cash-count claims for the same shift, and full monitoring coverage beside its own record of a five-hour blackout. Under this project's governing rule — *a wrong number is worse than no number, because a wrong number gets believed* — these cannot be shown to an owner.
2. **The central architectural claim of Phase 1 is false.** My Phase 1 report stated that swapping `SHIFT_DOMAIN` for a live read would require no change below it. It would not: at least eight other constants live outside that boundary, and an empty result set crashes the page (`SHIFTS[0]` → `undefined` → TypeError on the next line). The readiness question this audit exists to answer is therefore **no**.
3. **Several fixes violate the locked backend rather than match it** — including a rule `metrics.py` marks with an explicit ⚠️, an alert type that is not in the public taxonomy, and four alert titles that diverge from `report.build_alert`.

The **text layer is genuinely solid** — Arabic strings, status logic and summary trees are faithful transcriptions, verified character-exact. The **number layer is not**. The fix list is enumerable and requires no redesign.

---

## 2. What Was Verified

| Area | Method |
|---|---|
| Backend/locked-file safety | `git diff HEAD` per file, byte-level |
| Release artifact safety | SHA-256 before and after the test run |
| Full test suite | `pytest -q` (604 tests) + root-cause analysis of failures |
| Financial arithmetic | All 10 shifts + all 7 trend days recomputed from the file via Node |
| Chart integrity | Live DOM measurement (`getBoundingClientRect`) in Chromium |
| Console health | Playwright console + pageerror listeners, all 7 screens |
| Responsive | Live viewports at 1440 / 768 / 375 / 320 px |
| Themes | Live toggle, `data-theme` assertion, screenshots both modes |
| Keyboard focus | Computed-style inspection of a focused nav button |
| Semantic fidelity | Independent adversarial agent vs locked files + 3 real Telegram reports |
| Security & secrets | Independent security agent across `dashboard/` |
| Supabase permissions | Independent agent across all 7 schema/grant files |
| Repo exposure | `gh repo view`, `git check-ignore`, `git ls-files`, `git grep` for secrets |
| Agent claims | Top-severity claims re-verified by me directly against source |

All 8 screens exercised across 10 mock shifts (80 render permutations).

---

## 3. What Is Correct

**Verified character-exact against the locked files** (independently re-checked):
- `deriveStatus()` is an exact transcription of `report.pick_status()`, including the subtle `gap_explained` short-circuit *ahead of* `has_notes`.
- All five `STATUS_*` strings and all nine `_SUMMARIES` bodies match exactly.
- The Case B heading and disclaimer are exact, including the `ℹ️` prefix; `showOther` correctly keys off `other_users.length > 0`, not a toggle.
- The formula line and `computeGrandTotal()` are correct; all 10 shifts are computed, never hardcoded.
- Line-item order matches `report.py` and all three real reports.
- `signed()` prints the sign at zero (`−0 ج`), matching real reports.
- `arInvoiceCount` matches `_ar_invoice_count` for all n.
- Note strings match `report.note_line` exactly.
- The 5% flat threshold matches `COMPARISON_FLAT_PCT`.
- Cashier names `حمص` / `محمود` correctly assigned to mornings/evenings.
- `assert_no_accusation` would pass — no banned word anywhere.
- Six of seven trend days reconcile exactly.
- `no_sales` (125 > 120 min) and `db_size` (83% > 80%) alert arithmetic is correct.

**Runtime (measured):** zero console errors, zero warnings; zero horizontal overflow at 1440/768/375/320; both themes work and persist; RTL correct; mobile restructures rather than forking the design.

**Project safety — all byte-identical to HEAD (`0c59084`):** `metrics.py`, `report.py`, `events.py`, `adapter_hdsoft.py`, `sqlguard.py`, `rows.py`, `agent.py`, `orchestrator.py`, `delivery.py`, `supa.py`, `schema.sql`, `test_golden.py`, `notifier/telegram.py`, `.github/workflows/delivery.yml`, `install/update_agent.ps1`, `UPDATE_POSENTINE.bat`, `make_ship.py`, all six `schema_v*.sql`. **Zero tracked modifications repo-wide.** The v1.0.3 artifact still hashes to the pinned `5629db77…` after the test run.

---

## 4. Issues Found

### 🔴 Critical — visible self-contradictions

**C1 — The trend chart asserts 11,265 ج for a day whose shifts total 6,580 ج (Δ 4,685).** ⚠️**self-inflicted.** `DAYS` is a constant independent of `SHIFT_DOMAIN`. Recomputed with the dashboard's own formula: 11 Aug ✅, 12 Aug ✅, **13 Aug ✗ 6,580 vs 11,265**, 14 Aug ✅, 15 Aug ✅. I converted the 13 Aug morning shift to all-zero `NO_DATA` in Phase 1 and never updated `DAYS`. The same UI labels that shift *«لا توجد بيانات كافية لهذه الوردية»* while the chart claims revenue for it. Six of seven bars reconciling makes the seventh worse — it reads as verified.

**C2 — Two screens report different totals for the same period.** Cashbook footer = **54,315 ج** for «إجمالي الفترة — 10 وردية»; the trend chart's same five days = **59,000 ج**. Also `trend.avg` («متوسط 11,731 ج / يوم») averages a series containing the wrong Thursday *and* two days (الأحد 9، الاثنين 10) with **no backing shifts at all** — `SHIFT_DOMAIN` covers only 11–15 Aug. Two of seven bars and the headline average are unsourced.

**C3 — Opposite cash-count claims for the same shift.** The Alerts page states «لم يتم جرد الخزينة في هذه الوردية» for «المساء — 15 أغسطس»; that exact shift (s1) carries `cash_counted:true`, so the Overview renders «تم تأكيد بيانات الخزينة». The alert's «قيمة النظام 6,715 ج» matches nothing (total 7,015; sales 6,480; sales+collections 7,720) — a fabricated figure one line from the real one.

**C4 — A zero-invoice shift displays a ranked top-5 product list and a full-coverage guarantee.** `detail.items` is the module constant `ITEMS`, never indexed by shift, so all ten shifts show identical rankings — including s6, whose own badge reads «لا توجد بيانات كافية» and whose counters are all 0. Its `coverageNote` fallback also asserts «تغطية كاملة … 240 نبضة متصلة بدون فجوات». `report.py` states the opposite: «صفر فواتير في نافذة الوردية = مراقبناش الوردية دي».

**C5 — 100% coverage claimed beside the same UI's own five-hour blackout.** `health` is hardcoded (`coverage:'100%'`, `beats:'240'`, «بدون فجوات»). The current shift is evening (19:00→07:00), yet the Monitoring page's beat chart marks hours 2–6 as «خارج نطاق الرصد» — five of its twelve hours. Both cannot be true. Separately, a nightly 02:00–06:00 blackout is not a real product behaviour (`events` has explicit night thresholds precisely because monitoring runs through the night), and that chart has **no hour axis**.

**C6 — The 7-day chart renders every bar at an identical height.** Measured at 1440px: inline heights range 86.04%→100%, but `computedHeight`/`barRectH` = **144px for all seven**, `distinctBarHeights: [144]`, `distinctBarTops: [889.8]`. A 12,905 ج day looks exactly like a 10,470 ج day. Cause: the percentage cannot resolve against a flex-column parent. **Diagnostic contrast:** the Health page's beat chart uses an explicit `height:74px` parent and works correctly (58% → 42.9px), isolating the defect.

### 🟠 High

**H1 — The Phase 1 adapter-boundary claim is false.** ⚠️**self-inflicted.** The header comment says swapping `SHIFT_DOMAIN` suffices. In fact `DAYS`, `ITEMS`, `ALERTS`, `users`, `products`, `productSplit`, `healthCards`, `healthLog`, `beats` and `health` all live outside it. Concrete failure modes on a real read: `SHIFTS[0]` → `undefined` → **TypeError, blank page** on an empty result; `ITEMS[0].qty` → same; `Math.max(...DAYS.map(…))` on empty → `-Infinity` → every bar `NaN%`; `countUp()` reads `SHIFTS[0].total` unguarded.

**H2 — The comparison sign violates the one rule `metrics.py` flags with ⚠️.** ⚠️**self-inflicted.** `metrics.py:315`: `# ⚠️ المعروض دايماً القيمة المطلقة — الاتجاه في الكلمة مش في الإشارة`, returning `abs(pct)`; `comparison_text_ar` renders «📈 أعلى بـ 5.6%» with no sign. My Phase 1 code emits **«+5.6%»**. It also prints a percentage in the `flat` case, where the locked file deliberately prints none. *(Verified by me directly.)*

**H3 — `cash_no_count` was given an owner-facing alert card, and a comment I wrote asserts it belongs.** ⚠️**self-inflicted.** `events.PUBLIC_TYPES` = `{zero_invoice, refund, cash_diff, deleted_invoice, no_sales, db_size}` — `cash_no_count` is absent, is `level=3`, and is a *report line* (`report.cash_line`), never a pushed alert. It also inflates the nav badge to 7. *(Verified by me directly.)*

**H4 — Four of five alert titles diverge from `report.build_alert`.** ⚠️**self-inflicted.** Real: «🔴 فرق في الخزينة — يستحق المراجعة», «⚠️ فاتورة محذوفة — تحتاج مراجعة», «⚠️ مفيش مبيعات مسجّلة». Dashboard: «يوجد فرق يستحق المراجعة» (this is `STATUS_CASH`, a *shift status*, misused as an alert title), «فاتورة اختفت من السجل», «لا توجد مبيعات منذ فترة أطول من المعتاد». *(Verified by me directly.)*

**H5 — Alert timestamps place events in the wrong shift window.** `resolve_shift`: morning `[07:00,19:00)`, evening `[19:00,07:00)`. s1 is **evening** yet owns notes for receipts timestamped «6:40 م» and «5:12 م» — both *morning* times — while s2 (that morning) has `notes:[]`. A 120 ج return and s1's entire REVIEW status sit on the wrong side of the 19:00 boundary. The `deleted_invoice` alert (13 Aug «3:20 م») lands in s6, the zero-invoice shift, and is internally inconsistent: first seen «12:55 م» + «35 دقيقة» = 1:30 م ≠ 3:20 م.

**H6 — The Users page inverts both users' roles relative to the data beneath it.** `محمود` is `primary_user` on **5** shifts (all evenings) but is labelled «نشاط مستخدم آخر»; `حمص` is primary on **4** but labelled «المستخدم الأساسي». Counts are fabricated and inverted by an order of magnitude (حمص «682» invoices vs 254 actual; محمود «34» vs 486 actual); «58,940 ج» matches no computable quantity.

**H7 — Product names are fictional and wrong for this customer.** The restaurant is Levantine (`مطعم صبح للمأكولات الشامية`); real top sellers across all three reports are `طعمية`, `بطاطس كاتشب`, `فول`, `بطاطس ثومية`, `بطاطس`. The dashboard lists an American fast-food menu (`فرايد تشيكن ساندوتش`, `برجر دبل`, `كرسبي ستريبس`), which returns **0 hits** in the customer's exported POS data (`طعمية` returns 56). It also leaks into an alert body. Additionally the per-shift quantity (42 × 10 shifts = 420) exceeds the 7-day total for the same item (286).

**H8 — The hero composition bar's 100% is not the total.** Its segments sum to **8,425** while the displayed total is **7,015**, and deductions (دليفري/مسحوبات/مرتجع) are drawn with *positive* width as if they were constituents. No formula decomposes 7,015 this way.

**H9 — Real customer values were re-dated onto the wrong shifts.** The mock mirrors real dates and cashiers, inviting direct comparison with the owner's Telegram archive, then disagrees with every one (e.g. real 15 Aug morning: محمود 13 فاتورة / 730 ج, total 21,665 — dashboard: 4 فواتير / 155 ج, total 5,740). The real pair «حمص — 2 فاتورتين — 135 ج» (14 Aug evening) has been relocated to 12 Aug evening. Scale is ~3× low throughout.

**H10 — No focus indication anywhere (WCAG 2.4.7 failure).** Every `<button>` carries inline `all:unset`, which resets `outline-style` to `none` and, as an inline author declaration, outranks the UA `:focus-visible` rule. Measured on a focused nav button: `outlineStyle:"none"`, `boxShadow:"none"`. `focus` appears **0 times** in 1,164 lines; `aria-` appears **0 times**. The app is fully keyboard-operable and completely un-navigable by keyboard.

**H11 — No loading, error, empty or unavailable states.** `loading`/`error`/`fetch`/`await`/`async` all appear 0 times; the only `try/catch` guards `localStorage`. For a *monitoring* product the important consequence is that a stale dashboard is indistinguishable from a live one — a dead agent still renders «الوكيل متصل · آخر نبضة قبل دقيقة» (hardcoded).

**H12 — Real owner PII in a non-gitignored folder inside a repository verified PUBLIC.** `gh repo view` → `"visibility":"PUBLIC"`. The dashboard hardcodes the owner's full name and role in persistent chrome; `dashboard/` is untracked **and not gitignored**, so `git add -A` publishes it. **The G-Brain vault records this repo as private — that context is stale and now corrected.**
*Correction to an agent finding:* the adversarial audit called this name "fabricated." It is not — it is the real client identity **you supplied in the brief**. The exposure risk is real; the "invented" characterisation is not. *(It does, however, collide with `محمود` the cashier shown elsewhere in the same UI.)*
*Verified separately:* `Docs/PHASE_2_DELIVERY_PLAN.md` (which the vault flags as holding an unrotated Telegram token) is **not committed**; the only JWT-shaped strings in the tracked tree are synthetic fixtures (one decodes to `{"role":"anon"}`, no project ref). **No real credential is exposed in the public repo.**

**H13 — `signed_money`'s mandatory عجز/زيادة descriptor is dropped.** `metrics.py:345` (`label = "عجز" if v < 0 else "زيادة"`) exists because the owner must know *shortage or surplus*. The dashboard renders «الفرق −640 ج»; `(عجز)`/`(زيادة)` appear **0 times**. The Overview omits the difference line entirely, leaving the owner to subtract. *(Verified by me directly.)*

**H14 — `is_partial` is not modelled, producing exactly the screen the schema forbids.** `schema.sql` states such shifts are recorded *«ومايتبعتش عنها تقرير للمالك»* (no report is sent to the owner). `is_partial` appears 0 times; s6 is precisely such a shift and is rendered as a full browsable card with a status badge, a coverage guarantee and a top-5 ranking.

**H15 — Settled end-of-shift verdicts presented ~2 minutes into a 12-hour shift.** The header says «جارية الآن» while the page asserts a final total, 97 invoices, a status verdict, 100% coverage, confirmed cash, and a settled week-over-week comparison. `report.build_shift_report` is an end-of-shift artifact.

### 🟡 Medium

- **M1** — Hardcoded `health` cannot react to a coverage gap; if a gapped shift were current, an orange «غير مكتملة» badge would sit above a green «100% … بدون فجوات».
- **M2** — Cash wording adds claims the locked file avoids: «— لا يوجد فرق يستحق المراجعة» is appended to a string deliberately reworded on 2026-08-13 *not* to assert reconciliation (it also fires for diffs below the 100 ج threshold). The same state is worded two different ways on two screens («لم يتم جرد الخزينة بعد … حتى الآن» vs the correct locked wording). ⚠️**self-inflicted.**
- **M3** — `n_other` unmodelled; «فواتير» = `total_invoices − n_other`, and a shift of only `other`-kind invoices shows 0 while the backend has `has_data:true`.
- **M4** — Dead branches: `stable:up`, `stable:down`, `stable:none` (the last is what the real 15 Aug report actually used); the comparison-unavailable branch is unreachable although **all three real reports show it** — production's normal case is the one that never renders. Two of three unavailable *reasons* are unmodelled entirely.
- **M5** — The headline total animates through incorrect values (`6,272 → … → 7,015`) for ~400 ms beside a panel already showing the correct figure.
- **M6** — No `lang` attribute; a screen reader announces the entire Arabic UI with an English voice.
- **M7** — Trend bars use a 26% floor (a 0 ج day still renders a bar), compressing a real 17% spread to ~10%; value labels are `display:none` below 640px, leaving unlabelled, axis-less, distorted bars on mobile.
- **M8** — Alerts badge shows 7 with no notion of `DAILY_ALERT_CAP` (3) or overflow deferring to the shift report.

### 🔵 Low / ⚪ Cosmetic

Arabic plural rule applied to invoices but not shifts («10 وردية» should be `ورديات`); `money()` bypassed for user amounts (`1955` vs `1,955`); zero-price alert arithmetic 144 vs stated 145; `gap_explained` newlines flattened despite the template already having `white-space:pre-line`; cashbook sign asymmetry (deductions signed, additions bare — `report.py` signs both); `renderVals()` mutates the DOM as a side effect; `_heroCleanup` listener leak on screen change; `countUp()` triggers ~60 full re-renders/second; first-paint theme flash (light → dark); `money()` emits ASCII `-` while the file uses U+2212 `−`; unused `uploads/pasted-*.png` (an unrelated developer-portfolio screenshot) and `.thumbnail`; `github.md` still claims the repo has no frontend code.

### ⚫ Process finding — false evidence in a verification claim

Two items belong in their own category because, in this project, they are the most serious kind of error:

1. **A code comment I wrote cites evidence that does not exist.** It claims `«2 فاتورتين / 4 فواتير / 13 فاتورة / 26 فاتورة all appear verbatim in the 3 real reports»`. **`4 فواتير` appears 0 times** in any of them — it came from my own mock. *(Verified by me directly: `grep -c` returns 0, 0, 0.)* Three of the four are genuine; the fabricated one sits inside a comment presented as a verification record.
2. **My Phase 1 harness reported "ALL RENDER PATHS CLEAN" across 80 permutations — and passed every defect above.** It only detected `NaN`/`undefined` leakage, then that result was reported as broad validation. This is precisely this project's recurring failure shape: *a check that shares the fault it is meant to detect.* The correct question — *what would this check still pass if it were broken?* — was not asked.

---

## 5. Business Logic Verification

| Concern | Result |
|---|---|
| Financial formula | ✅ Correct — computed, never hardcoded; all 10 shifts consistent |
| User activity Case A/B | ✅ Correct in mechanism (data-driven, never added to total, exact disclaimer) — ❌ but the Users **page** inverts both roles (H6) |
| Shift statuses | ✅ Correct — all five, exact text, exact priority order |
| Coverage | ⚠️ Logic correct (explained vs unexplained) — ❌ but contradicted on screen (C5) and unmodelled for `is_partial` (H14) |
| Withdrawals | ✅ Correct — first-class, correctly signed, never "expenses" |
| Alerts | ❌ Taxonomy breached (H3), titles diverged (H4), timestamps in wrong windows (H5) |
| Summaries | ✅ Character-exact — ❌ three branches dead (M4) |
| Comparisons | ❌ Sign rule violated (H2); production's normal case unreachable (M4) |

---

## 6. Real Customer Data Verification

✅ **Confirmed exact** against the three real reports: financial line labels and order, formula footnote, signed zero (`−0 ج`), all status wording, the incomplete-shift summary, the Case B heading and `ℹ️` disclaimer, Arabic invoice-count grammar, and the two real cashier names.

❌ **Contradicted by the real reports:** product names (H7), per-shift/aggregate user figures (H6), alert titles (H4), the comparison sign and its default state (H2/M4), and every value/date pairing (H9).

**Real evidence vs deterministic mock — explicit separation:**
- **Real:** all Arabic labels/wording/status/summary strings, the formula, invoice-count grammar, the two cashier names, the owner's name (from your brief), shift window times.
- **Mock:** every monetary amount, every invoice count, all product names and quantities, all alerts and receipt numbers, all health/heartbeat figures, all per-user aggregates.

No real customer financial figure is presented. The real reports validated *structure and language* — but H9 shows the mock mimics real dates and cashiers closely enough to invite false comparison, which is itself a risk.

---

## 7. Security Verification

| Check | Result |
|---|---|
| Service-role key / anon key / any JWT | ✅ None |
| DB password, connection string, bot token, API key | ✅ None |
| Browser-side DB access | ✅ None — no Supabase/SQL reference exists |
| POS write path | ✅ None |
| Data source | ✅ 100% in-file constants |
| Storage | ✅ Only `localStorage['posentine-theme']` |
| Real credential in the **public** repo | ✅ None |
| **Real PII in a public-repo working tree** | ❌ **H12** |

**External network calls — yes, three third-party origins** (the brief asked directly): `unpkg.com` (React/ReactDOM, unconditional, **SRI correctly applied**); `fonts.googleapis.com` / `fonts.gstatic.com` (no SRI possible, leaks visitor IP/User-Agent); and a same-origin `fetch(location.href)` hot-reload self-check. None transmits dashboard data; there is no telemetry or exfiltration path.

**Future-deployment concerns:** the runtime executes logic via `new Function()`, so any production CSP must permit `'unsafe-eval'`; no self-hosted fallback for the CDN; `support.js` contains an editor bridge that `postMessage`s with a wildcard target origin; no auth or tenant scoping exists yet.

---

## 8. Supabase Readiness

Net effective grants after `schema.sql` → v2 → v3 → v7:

| Table | Needed for | `anon` | `authenticated` | `service_role` |
|---|---|---|---|---|
| `shift_reports` | Overview, Shifts, Detail, Cashbook | ❌ | ❌ **none** | ALL |
| `events` | Alerts | ❌ | ❌ **none** | ALL |
| `tenants` | Restaurant name, timezone, currency | ❌ | ❌ **none** | ALL |
| `internal_anomalies` | Monitoring detail | ❌ | ❌ **none** | ALL |
| `withdrawals` | Withdrawal detail | ❌ | ✅ SELECT/INSERT/UPDATE/**DELETE** | caveat |
| `heartbeats` | Coverage/health | ❌ | ✅ SELECT/INSERT/UPDATE | ALL |
| `invoices`, `invoice_lines` | Products, detail | ❌ | ✅ SELECT/INSERT/UPDATE | ALL |
| `pos_users`, `pos_products` | Users, Products | ❌ | ✅ SELECT/INSERT/UPDATE | ALL |
| `cash_counts` | Treasury states | ❌ | ✅ SELECT/INSERT/UPDATE | ALL |

**The blocker, precisely.** The four tables carrying everything the dashboard exists to display — `shift_reports`, `events`, `tenants`, `internal_anomalies` — are blocked **twice**: (1) no table-level GRANT to `anon`/`authenticated`, and Postgres checks privileges *before* RLS, so this alone yields `403/42501`; (2) RLS is enabled with **zero policies**, so even with a grant they would return zero rows.

`anon` holds no privilege on any table anywhere. `service_role` can read everything but bypasses RLS, and `schema.sql` states in its own comment that it is *"used from GitHub Actions only, and absolutely forbidden to place on the customer's machine."*

**Does the schema already support the dashboard unmodified?** For ingestion tables, yes (tenant-scoped via an `authenticated` JWT). For the reporting tables the dashboard actually displays, **no**.

**Two decision-relevant facts:**
- The raw ingredients *are* reachable by an `authenticated` JWT, so a dashboard could technically recompute shift figures client-side — but that would duplicate the authoritative calculation in the frontend, which your brief forbids and which the golden-test discipline exists to prevent. Stated as fact, not suggestion.
- `withdrawals` is the **only** table granting `DELETE` to `authenticated`. Any browser credential of that role is write-capable, not read-only.

**Not determinable from the files:** whether `service_role` holds privileges on `withdrawals` (v7 grants only to `authenticated`); and who mints the agent JWT / whether its `tenant_id` claim can be chosen by the holder — all tenant isolation rests on that claim.

**Whether a safe architecture exists:** yes, more than one — not proposed here, per your instruction.

---

## 9. Integration Risk Register

| # | Risk | Impact |
|---|---|---|
| R1 | Constants outside the adapter boundary (H1) | The "swap one array" plan is invalid; empty/failed reads crash the page |
| R2 | Static arrays keep rendering plausible values (C3–C5, H6, H7) | A half-wired dashboard looks fully live while showing fiction — the false-green failure this project has repeatedly hit |
| R3 | Reporting tables unreachable by any browser credential | Blocks Overview/Shifts/Cashbook/Alerts entirely |
| R4 | Client-side recomputation temptation | Would duplicate authoritative logic and break single-source-of-truth |
| R5 | `is_partial` unmodelled (H14) | Shows shifts the backend guarantees are never reported |
| R6 | `withdrawals` grants DELETE to `authenticated` | A read-only path must not reuse that role as-is |
| R7 | Tenant isolation rests on a JWT claim | Must verify who can mint it, and with what claims |
| R8 | PII + public repo (H12) | Real owner name published on first `git add -A` |
| R9 | Broken chart (C6) | Will misrepresent real data exactly as it misrepresents mock |
| R10 | Production's normal comparison state unreachable (M4) | The one state customers see today is the one never rendered |
| R11 | unpkg / Google Fonts dependency | Availability + privacy exposure for an owner-facing tool |
| R12 | CSP requires `'unsafe-eval'` | Constrains production hardening |
| R13 | Timestamps in wrong shift windows (H5) | Attribution errors survive into real data if the pattern is copied |

---

## 10. Recommended Next Step

**Recommendation only — not executed.**

1. **Immediately, independent of any integration decision:** add `dashboard/` to `.gitignore` (or move the owner name to a runtime value) so the public repo cannot receive real PII on a routine commit (H12).
2. **Fix the contradictions before this is shown to the owner** — C1–C6 plus H6/H7. These are wrong today regardless of architecture, and they are the items that would damage trust in the product.
3. **Correct the locked-file deviations I introduced** — H2, H3, H4, H13, M2 — and remove the false evidence claim in the code comment. These are small, but they are the difference between "matches the backend" and "looks like it matches."
4. **Then make the integration decision** using §8. The binding constraint is that the four tables the dashboard exists to display are unreachable by every browser-available credential; no amount of dashboard work resolves it.
5. **Defer H11** (loading/error/empty states) until the data-access architecture is chosen — their shape depends on it.

I'd also suggest treating "the mock must be internally consistent" as a standing rule: every one of C1–C5 is a case where two constants disagreed, and a viewer can see both at once.

---

## Test Results

**`pytest -q`: 602 passed, 2 failed (604 total).**

Failures: `test_update_agent.py::test_bat_stops_cleanly_when_the_updater_is_not_next_to_it` and `::test_bat_stops_cleanly_in_the_extracted_delivery_folder`.

**Root cause — environmental, not a regression, not dashboard-related.** Both run `cmd /c UPDATE_POSENTINE.bat` relying on the working directory to resolve the batch file. This environment sets `NoDefaultCurrentDirectoryInExePath=1`, which stops `cmd.exe` resolving from the CWD. Captured stderr is exactly `'UPDATE_POSENTINE.bat' is not recognized as an internal or external command`, with empty stdout, so the output assertions fail. Reproduced independently in a native PowerShell session and confirmed by reading the env var.

Supporting evidence: `UPDATE_POSENTINE.bat` and `install/update_agent.ps1` are **byte-identical to HEAD**; the dashboard is an untracked file these tests never read; the other 24 tests in the same file pass; and the vault records **604 passed** at v1.0.3.

**No test was modified.**

**Other results:** browser console 0 errors / 0 warnings; no horizontal overflow at any of 4 breakpoints; both themes and persistence ✅; 10/10 shifts formula-consistent; **1 of 5 days inconsistent** (C1); all 7 trend bars measured identical (C6); focus indicator absent (H10); release artifacts SHA-256 identical before and after.

---

## Repository State

- Branch `main` @ `0c59084`, **zero tracked modifications**.
- Exactly **one** dashboard: `dashboard/POSentine Arabic Dashboard/`. The obsolete duplicate was removed last session (backed up outside the repo). No conflicting version, no broken reference; `support.js` present and unmodified.
- `dashboard/` is **untracked and not gitignored** (H12).
- Nothing committed, nothing pushed, no Supabase/RLS/grant/auth/schema change, no deployment, no customer-machine contact.

---

> **POSentine DASHBOARD — PRE-SUPABASE VERIFICATION COMPLETE — AWAITING HUMAN DECISION**
