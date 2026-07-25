# Jaipur Rugs Chatbot — Founder OP Assessment & Fix Plan

**Date:** 2026-07-25  
**Owner:** Engineering (current JR bot owner)  
**Audience:** Founder / product leadership  
**Repos:** Keep current architecture. Do **not** rebuild from scratch on Kisna. Fix product search to site-parity quality, then harden conversation / WhatsApp / catalog sync.

---

## 0. Executive answer (read this first)

### Is the current architecture good enough?

**Yes — keep it.** Rewrite from scratch (Kisna-style greenfield) is the wrong move for JR.

| Question | Answer |
|----------|--------|
| Rewrite whole chatbot like Kisna? | **No.** Different catalog (Mongo + rug color/size vs Clara jewellery API). JR already has the portable Kisna ideas (progressive relax, honest notes, extract hygiene). |
| Is search “done”? | **No.** Search is the incomplete core. Conversation, WA channel, KB, dashboard are secondary. |
| Biggest risk today? | Shipping “looks fine” demos while **PLP filters ≠ bot filters**, and some website SKUs **never appear in Product Master API**. |
| Path to “perfect”? | Treat search as a product with a **query plan + parity tests + diagnose tooling**, not more prompt patches. |

### One-line recommendation

> **Stabilize and finish the JR stack you have. Steal Kisna’s discipline (extract → evidence → drop-one-filter → honest note), not Kisna’s jewellery agents. Make product search match jaipurrugs.com PLP behavior on the Product Master catalog we actually get.**

---

## 1. Critical verdict: rewrite vs continue

### Why previous attempts failed (pattern, not people)

From the codebase and live failures, past work likely stalled because:

1. **No single source of truth for “what the user asked”** — agent tool keyword + regex + LLM extract + previous search all fight each other → bleed and empty results.
2. **Website PLP ≠ Product Master API** — demos compare bot to website chips; some rugs (e.g. LAAL CHATTAN) are **website-only** and will never match until JR exposes them in API.
3. **Rug search is harder than jewellery search** — color families, yarn %, size groups (5×8 vs “medium”), multi-currency MRP, round diameters.
4. **Patches without a diagnose loop** — every bug needed a developer + Mongo spelunking; no 1-minute “LOGIC / MONGO / API_GAP” check until recently.

### Why Kisna arch is *not* a full template for JR

| Dimension | Kisna | JR today |
|-----------|-------|----------|
| Catalog | Clara HTTP filters (category, material, price) | Mongo Product Master + tokens + color tiers + yarn breakdown |
| Domain | Karat, metal, occasion, mangalsutra ontology | Shape, SizeGroup, ColorFamily, rooms, constructions |
| UX shell | Multi-agent sticky product_search session | Single OpenAI agent + tools (web + WA) |
| What to steal | Context-free extract, evidence gate, drop-one strategy + honest note | Already partially ported in `jaipur_rugs_api.py` / `jr_search_llm_extract.py` |
| What not to steal | Jewellery agents, Clara entity shape, checkout/karat flows | — |

**Rewrite cost:** throw away rug-specific ranking (color tiers, breakdown, size chips), re-implement WA/web unify, months of regression — **without fixing API/website gap**.

**Continue cost:** focused search parity + sync discipline + honesty UX — weeks, measurable against PLP.

---

## 2. Current architecture (what we keep)

```text
Web (WS) ──┐
           ├── FastAPI agent (OpenAI tools) ──► Mongo JR.products (synced Product Master)
WhatsApp ──┘         │
                     ├── search_kb (Pinecone policies)
                     ├── stores / alerts / show-more buffer
                     └── daily sync: JR product-master → Mongo
```

**Strengths (do not discard):**

- One backend for web + WhatsApp (Vultr) — correct ops model.
- Catalog cached in Mongo (latency + offline-from-JR-search) — correct for chat.
- LLM-primary extract + hygiene against context bleed — right direction.
- Progressive filter relax + fallback notes — Kisna-aligned.
- Diagnose script: `scripts/diagnose_catalog.py` → `API_GAP` / `MONGO` / `LOGIC`.

**Weaknesses (fix in place, don’t rewrite):**

- Search pipeline is a **stack of overlapping relax paths** (hard to predict which note wins).
- “Medium” / “Red” definitions **diverge from website chips**.
- Agent can still sound exact when filters were relaxed (web worse than WA intros).
- Product Master feed **≠** full website CMS.

---

## 3. Product search — the main battle

### 3.1 How search works today (simplified)

1. User message → agent tool keyword  
2. Hygiene (drop polluted prior filters)  
3. LLM extract → `clean_keyword` + attrs + price  
4. Mongo `$and` recall on tokens / raw fields  
5. Hard post-filters (shape, size bucket, material, …) + color tiers  
6. If empty: drop size → shape → room → … → widen/drop price  
7. Rank → return 3 (+ show-more pool) + optional honesty note  

### 3.2 Proven failure modes (live / Mongo / API)

| # | Symptom | Root cause | Bucket |
|---|---------|------------|--------|
| 1 | Website shows rug (e.g. LAAL CHATTAN); bot never can | SKU **absent from Product Master** (15258 SKUs scanned) | **API_GAP** |
| 2 | User asks red + round + USD band; bot drops round, shows rectangles | Strict `red` ignores ColorFamily “Red and Orange”; few hard-red rounds in band | **LOGIC** |
| 3 | “Medium” wrong vs site | Our medium = 48–120 sqft → **5×8 counted as small**; site medium ≈ 5×8–8×10 chips | **LOGIC** |
| 4 | Stacked honesty notes / sticky size_relaxed | Intro concatenated size + shape notes; size_relaxed set before winning drop | **LOGIC** (partially fixed locally) |
| 5 | Prior-turn bleed (`aurelia` + `new arrival`) | Tool keyword / previous search trusted too much | **LOGIC** (improved; needs regression suite) |
| 6 | Soft phrasing (“is there any new arrival”) | Regex-blind; LLM must own intent | **LOGIC** (llm_primary + prompt) |
| 7 | Empty Mongo `$and` before relax | Hard segment AND exited early | **LOGIC** (mitigated with segment drop) |
| 8 | `$gte` price widen raised floor | Widen multiplied instead of divided | **LOGIC** (fixed) |
| 9 | Soft-keep material/pattern + claim match | Soft keep without note | **LOGIC** (hard-miss + progressive) |
| 10 | Stale Mongo after API add | Sync not run / orphan delete edge cases | **MONGO** |

### 3.3 Definition gaps vs website PLP (`/in/rugs`)

Website filters are **chips**. Bot historically used **invented buckets**.

| Concept | Website | Bot today | Fix |
|---------|---------|-----------|-----|
| Medium | SizeGroup chips ~5×8, 6×9, 8×10 (+ dia rounds in marketing) | sqft 48–120 (5×8 = **small**) | Map medium → SizeGroup set + matching dia rounds |
| Red | Color chip includes family / coral / rose often | Strict GrColor / tokens; ColorFamily often excluded | Include ColorFamily / DisplayFilter for recall when shape+price tight |
| Price | INR buckets on `/in`, USD on `/us` | User-stated currency (good) but demos mix INR PLP vs USD chat | Detect locale; document demo currency |
| In stock | Ready to ship chip | `LiveStatus && Published` | Align QuickShip if API exposes it |

### 3.4 Target search behavior (“perfect”)

For every shop-by query:

1. **Parse once** — user message is SoT; previous only on explicit refine / show-more / budget-only.  
2. **Recall broadly, filter honestly** — prefer site-chip semantics.  
3. **If exact AND is empty** — drop one constraint at a time (Kisna order), **one** note naming what dropped.  
4. **Never claim** attributes that were dropped.  
5. **Diagnose in &lt;1 min** — `LOGIC` vs `MONGO` vs `API_GAP` before blaming the model.  
6. **Parity tests** — golden queries vs Mongo fixtures + expected SKUs / notes.

### 3.5 Search fix plan (phased)

#### Phase A — Site parity filters (P0, 1–2 weeks)

1. **Medium = website chips**  
   - Implement SizeGroup match: `5X8`, `6X9`, `8X10` (+ agreed dia rounds).  
   - Stop using 48–120 sqft as primary medium definition.  
   - Files: `jr_search_sizes.py`, extract prompt, tests.

2. **Red / color family recall**  
   - When user says a primary color, allow ColorFamily / soft catalog labels (site-like), especially with shape+price.  
   - Keep nearest-first ranking so pure GrColor still wins when present.  
   - Files: `jr_search_mongo.py`, `jr_search_index.py` (`build_search_tokens`), recommendation.

3. **One honesty note**  
   - Ship `build_search_intro` single-note behavior + size_relaxed only on winning size drop.  
   - Push pending local note fix if not already on `jr-production`.

4. **Golden query suite**  
   - Cases: soft new arrival; aurelia+red; red+round+USD band; medium+5×8; show-more; Hindi color.  
   - Assert: extracted attrs, whether shape dropped, expected SKU class (e.g. PAE-5080 for red-family round ~$18k).

#### Phase B — Query-plan clarity (P1, 1–2 weeks)

5. **Single strategy loop API** (Kisna-shaped, JR-filled)  
   - Explicit list: `(filters, keyword, price, note_kind)` tried in order.  
   - Log `strategy=drop_shape` etc. into debug / TRACE for web debug panel.  
   - Collapse overlapping “size re-query” + “progressive” into one loop.

6. **Evidence gate on extract**  
   - Strip attrs not evidenced in current user text (Kisna `apply_llm_evidence_gate`).  
   - Strengthens bleed protection beyond hygiene.

7. **Currency / locale contract**  
   - Web: respect stated currency; default from country.  
   - Document: “Demo on `/in` with INR filters ≠ USD chat query.”

#### Phase C — Catalog truth (P0 ongoing, parallel)

8. **API vs website gap process**  
   - Any “website shows X, bot doesn’t” → `diagnose_catalog.py --find … --full-api`.  
   - `API_GAP` → ticket to JR (Product Master), not bot rewrite.  
   - `MONGO` → run sync.  
   - `LOGIC` → Phase A/B bug.

9. **Sync reliability**  
   - Monitor daily cron; alert on synced count drop.  
   - After tokenizer changes, mandatory `backfill-search-tokens`.  
   - Avoid silent orphan wipe on partial API failures (guardrails).

10. **Optional: secondary website feed** (only if JR won’t fix API)  
    - Last resort crawl/sitemap for missing PLP SKUs — high maintenance; prefer JR API completeness.

---

## 4. Whole-chatbot issue map (beyond search)

| Area | Status | Issues | Fix approach |
|------|--------|--------|--------------|
| **Product search** | Incomplete (P0) | See §3 | Phases A–C |
| **Context / memory** | Improved | Bleed on soft asks; show-more / refine edge cases | Evidence gate + session TTL (exists) + golden turns |
| **WhatsApp** | Mostly usable | Image CDN needs Cloudinary; ordinals/price captions; rate lock | Ops checklist + Kisna-style image wrap (exists) |
| **Web chat UX** | Usable | Honesty notes less consistent than WA intro | Force intro from `fallback_note` in formatter, less LLM paraphrase |
| **KB / policies** | Adequate if ingested | Pinecone empty → weak answers; tool naming confusing | Ingest policies; smoke tests |
| **Stores / alerts / handoff** | Adequate | Not blocking “perfect shopper” | Maintain |
| **Observability** | Partial | TRACE exists; web debug not always strategy-clear | Expose strategy + verdict in bot debug JSON |
| **Catalog sync** | Critical dependency | API incompleteness; sync lag | Diagnose + JR escalation path |
| **QA / release** | Weak historically | No parity suite | Golden queries + diagnose in CI smoke |

---

## 5. What “perfect” means (acceptance bar)

Search is “good enough for founder/client demos” when:

1. For Product-Master-backed SKUs, bot finds them under the same chips the site uses (color / shape / size group / price currency).  
2. When exact match impossible, bot shows **closest** with **one clear sentence** (shape dropped / size widened / budget widened).  
3. No prior-turn bleed on fresh asks (`new arrival`, `aurelia in red`).  
4. Any mismatch diagnosed in &lt;1 minute as **LOGIC / MONGO / API_GAP** with `scripts/diagnose_catalog.py`.  
5. `API_GAP` items are tracked as **JR feed tickets**, not bot defects.

“Perfect” does **not** mean inventing website-only SKUs that Product Master never returns.

---

## 6. Recommended roadmap (for founder)

| Week | Outcome |
|------|---------|
| **0 (now)** | Agree: **no rewrite**. Search is the critical path. Use diagnose for client disputes. |
| **1–2** | Phase A: medium chips + ColorFamily red + single honesty note + golden tests. Push + demo red+round+USD band showing rounds like PAE-5080 when in catalog. |
| **3–4** | Phase B: unified strategy loop + evidence gate + debug strategy labels. |
| **Ongoing** | Phase C: sync monitoring + JR API_GAP escalation list (LAAL CHATTAN #1 evidence). |
| **Then** | Multilingual follow-ups, KB polish, WA polish — only after search parity bar is green. |

### Resource stance

- **Do:** 1 owner on search parity + tests; JR liaison for API gaps.  
- **Don’t:** Restart on Kisna multi-agent shell; don’t promise website-only SKUs until API includes them.

---

## 7. Client / founder communication templates

**Architecture**

> We are not rebuilding from scratch. The JR bot architecture (unified web/WA + Product Master → Mongo + LLM extract + progressive relax) is sound and already aligned with the proven Kisna search *discipline*. The remaining gap is product-search parity with the website filters and catalog feed completeness.

**When website shows a rug bot doesn’t (API_GAP)**

> We verified this SKU against the full JR Product Master API (~15k products) and our Mongo sync. It is not in the Product Master feed. The website can show CMS-only items. Until JR adds it to Product Master, no chatbot on this API can return it. Example: LAAL CHATTAN.

**When bot drops a filter (LOGIC, honest)**

> There were no exact matches for all filters combined in the catalog we receive. We showed the closest matches and stated what was relaxed (e.g. shape). We are tightening color/size definitions to match site chips so exact matches improve.

---

## 8. Immediate engineering backlog (priority ordered)

1. Ship pending honesty-note / size_relaxed fix (if not on `jr-production`).  
2. Medium → SizeGroup chip map (fix 5×8 = medium).  
3. ColorFamily-inclusive red recall + keep nearest-first rank.  
4. Golden query tests + CI smoke.  
5. Unify progressive strategy loop + debug `strategy=` field.  
6. Evidence gate on LLM extract.  
7. Sync health alert + API_GAP ticket list to JR.  
8. Web formatter always prefixes `fallback_note` (don’t rely on model).  
9. Expand diagnose script: `--query` + expected note kind.  
10. Only then: Hindi follow-ups / KB depth / WA niceties.

---

## 9. Decision log (ask founder to confirm)

| Decision | Recommendation | Founder confirm |
|----------|----------------|-----------------|
| Rewrite chatbot from scratch on Kisna arch? | **No** | ☐ |
| Current JR arch good enough to finish? | **Yes** | ☐ |
| #1 priority = product search site parity? | **Yes** | ☐ |
| Website-only SKUs = JR API responsibility? | **Yes** | ☐ |
| Accept “honest relax” when exact AND empty? | **Yes** (Kisna-style) | ☐ |

---

## 10. Appendix — key files & tools

| Piece | Path |
|-------|------|
| Search orchestration | `qlink_chatbot/utils/jaipur_rugs_api.py` |
| Mongo recall / post-filters | `qlink_chatbot/utils/jr_search_mongo.py` |
| LLM extract / hygiene | `qlink_chatbot/utils/jr_search_llm_extract.py` |
| Size / medium | `qlink_chatbot/utils/jr_search_sizes.py` |
| Ranking / honesty in reasons | `qlink_chatbot/utils/jr_search_recommendation.py` |
| Agent + tools | `qlink_chatbot/agent/chat_agent.py` |
| Sync | `qlink_chatbot/routes/dashboard_routes.py` |
| JR API client | `qlink_chatbot/utils/jr_api_client.py` |
| Diagnose (1 min) | `scripts/diagnose_catalog.py` |
| Kisna reference (steal discipline only) | `kisna-chatbot-0-text-flow-only/.../product_search_agent_v3.py`, `entity_extractor.py` |

### Diagnose cheat sheet

```bash
python scripts/diagnose_catalog.py --find "laal chattan" --full-api
# → API_GAP = not our bug

python scripts/diagnose_catalog.py --find "PAE-5080-0001"
# → IN_MONGO + has_red_token false = LOGIC (ColorFamily)

python scripts/diagnose_catalog.py --query "red round above 15000 usd below 20000 usd" --expect-sku PAE-5080-0001
# → proves bot should/shouldn't return that SKU
```

---

**Bottom line for the founder:** The architecture is good enough. The unfinished product is **search parity + catalog truth**. Finish that with Kisna’s *method*, not a Kisna *rewrite* — and separate bot bugs from JR Product Master gaps so demos stop chasing ghosts.
