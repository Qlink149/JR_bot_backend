#!/usr/bin/env python3
"""Extensive JR chatbot smoke suite - offline + optional live Mongo/LLM.

Usage (from JR_bot_backend root):

  # Offline only (no Mongo / OpenAI / Pinecone) - always run this first
  python scripts/extensive_smoke.py

  # Offline + pytest golden/strategy/KB/WA suites
  python scripts/extensive_smoke.py --pytest

  # Live bot search battery (needs .env + Mongo; LLM optional)
  python scripts/extensive_smoke.py --live

  # Full kitchen sink
  python scripts/extensive_smoke.py --pytest --live --live-llm

  # Print founder/manual checklist only
  python scripts/extensive_smoke.py --checklist
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SuiteReport:
    name: str
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(CheckResult(name=name, ok=ok, detail=detail))

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def _banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _ok(msg: str) -> None:
    _safe_print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    _safe_print(f"  FAIL  {msg}")


def _info(msg: str) -> None:
    _safe_print(f"  .     {msg}")


# ---------------------------------------------------------------------------
# Offline suites (no network)
# ---------------------------------------------------------------------------


def suite_strategies() -> SuiteReport:
    report = SuiteReport("strategies")
    from qlink_chatbot.utils.jr_search_strategies import (
        build_search_strategies,
        prefer_drop_size_over_exact,
        widen_price_filter,
    )

    filters = {
        "color_exact": {"red"},
        "shape": {"round"},
        "size_category": {"medium"},
        "room": {"living"},
        "material": {"silk"},
    }
    price = {"operator": "$lte", "amount": 20000, "currency": "USD"}
    ids = [s.id for s in build_search_strategies("red&round&medium&living&silk", filters, price)]
    report.add(
        "order starts with exact",
        ids[0] == "exact",
        f"ids={ids}",
    )
    report.add("has drop_size before drop_shape", "drop_size" in ids and ids.index("drop_size") < ids.index("drop_shape"))
    report.add("has drop_shape", "drop_shape" in ids)
    report.add("has widen_price then drop_price", "widen_price" in ids and ids.index("widen_price") < ids.index("drop_price"))
    report.add(
        "prefer_drop_size on weak exact",
        prefer_drop_size_over_exact(
            exact_results=[{"SKU": "x"}],
            exact_tier="breakdown_fallback",
            drop_size_results=[{"SKU": "y"}],
            drop_size_tier="exact_catalog_color",
        ),
    )
    widened = widen_price_filter({"operator": "$gte", "amount": 15000, "currency": "USD"}, 1.25)
    report.add("widen $gte lowers floor", bool(widened) and widened["amount"] == 12000, str(widened))
    return report


def suite_evidence_and_hinglish() -> SuiteReport:
    report = SuiteReport("evidence_hinglish")
    from qlink_chatbot.utils.jr_search_aliases import canonical_color_key
    from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
    from qlink_chatbot.utils.jr_search_llm_extract import (
        apply_llm_evidence_gate,
        build_search_payload_from_attrs,
        validate_extracted_attributes,
    )

    cases = [
        ("laal", "red"),
        ("hara", "green"),
        ("peela", "yellow"),
        ("bhura", "brown"),
        ("gol", None),  # shape, not color
    ]
    for token, expect in cases:
        if expect is None:
            continue
        got = canonical_color_key(token)
        report.add(f"alias {token!r}->{expect!r}", got == expect, f"got={got!r}")

    _pf, clean, _colors, filters = normalise_keyword("laal gol dari")
    report.add(
        "normalise laal gol -> red+round",
        "red" in (filters.get("color_exact") or set()) and "round" in (filters.get("shape") or set()),
        f"clean={clean!r} filters={filters}",
    )

    soft = apply_llm_evidence_gate(
        "is there any new arrival",
        {
            "colors": ["red"],
            "shapes": ["round"],
            "collection": "Aurelia",
            "catalog_tags": ["new"],
        },
    )
    report.add(
        "evidence gate strips aurelia bleed on soft ask",
        soft.get("colors") == []
        and soft.get("shapes") == []
        and soft.get("collection") in (None, "")
        and soft.get("catalog_tags") == ["new"],
        str(soft),
    )

    hinglish = apply_llm_evidence_gate(
        "laal gol dari",
        {"colors": ["red"], "shapes": ["round"], "collection": "Aurelia"},
    )
    report.add(
        "evidence gate keeps laal/gol -> red/round",
        "red" in (hinglish.get("colors") or []) and "round" in (hinglish.get("shapes") or []),
        str(hinglish),
    )

    attrs, _ = validate_extracted_attributes(
        {
            "colors": ["laal"],
            "shapes": ["gol"],
            "sizes_ft": [],
            "sizes_cm": [],
            "size_categories": [],
            "materials": [],
            "constructions": [],
            "patterns": [],
            "rooms": [],
            "catalog_tags": [],
            "multicolor": False,
            "weight_max_kg": None,
            "has_price_filter": False,
            "price_currency": None,
            "price_type": "none",
            "price_amount": None,
            "price_min": None,
            "price_max": None,
            "price_raw_phrase": None,
            "sku": None,
            "collection": None,
            "refinement": "new",
        },
        source_text="laal gol dari",
    )
    payload = build_search_payload_from_attrs(attrs)
    report.add(
        "validate laal/gol -> catalog red/round",
        attrs.get("colors") == ["red"] and attrs.get("shapes") == ["round"],
        f"attrs={attrs} kw={payload.get('clean_keyword')!r}",
    )
    return report


def suite_currency() -> SuiteReport:
    report = SuiteReport("currency")
    from qlink_chatbot.utils.jr_search_currency import (
        extract_price_filter_from_text,
        extract_requested_currency_from_text,
    )

    msg = "show me red rugs above 15000 usd and below 20000 usd round shape"
    report.add(
        "extract USD from founder demo query",
        extract_requested_currency_from_text(msg) == "USD",
    )
    report.add(
        "extract INR from explicit INR ask",
        extract_requested_currency_from_text("under INR 50000") == "INR",
    )
    pf, _ = extract_price_filter_from_text(msg.lower())
    report.add("price filter present on USD band", bool(pf), str(pf))
    if pf:
        report.add(
            "price filter currency USD",
            (pf.get("currency") or "").upper() == "USD",
            str(pf),
        )
    return report


def suite_medium_size() -> SuiteReport:
    report = SuiteReport("medium_size")
    from qlink_chatbot.utils.jr_search_sizes import product_matches_size_category

    five_eight = {
        "SizeInFT": "5x8",
        "SizeGroupInFT": "5X8",
        "Shape": "Rectangle",
    }
    smallish = {
        "SizeInFT": "2x3",
        "SizeGroupInFT": "2X3",
        "Shape": "Rectangle",
    }
    report.add(
        "5x8 matches medium chip",
        product_matches_size_category(five_eight, "medium"),
    )
    report.add(
        "2x3 does not match medium",
        not product_matches_size_category(smallish, "medium"),
    )
    return report


def suite_search_verdict() -> SuiteReport:
    report = SuiteReport("search_verdict")
    from qlink_chatbot.agent.chat_agent import _search_verdict

    report.add("empty -> empty", _search_verdict(strategy="exact", product_count=0) == "empty")
    report.add(
        "exact no note -> exact",
        _search_verdict(strategy="exact", product_count=3, fallback_note="") == "exact",
    )
    report.add(
        "drop_shape -> relaxed",
        _search_verdict(strategy="drop_shape", product_count=2, fallback_note="x") == "relaxed",
    )
    report.add(
        "widen_price -> relaxed",
        _search_verdict(strategy="widen_price", product_count=1) == "relaxed",
    )
    report.add(
        "mongo_drop with note -> relaxed",
        _search_verdict(strategy="mongo_drop_size", product_count=2, fallback_note="note")
        == "relaxed",
    )
    return report


def suite_wa_captions() -> SuiteReport:
    report = SuiteReport("wa_captions")
    from qlink_chatbot.routes.whatsapp_routes import _format_products_for_whatsapp

    products = [
        {
            "name": "Abrash",
            "image": "https://images.jaipurrugs.com/x.jpg",
            "url": "https://www.jaipurrugs.com/in/rugs/a",
            "size": "5x8",
            "material": "Wool",
            "display_price": "INR 14,700",
            "mrp": {"INR": 999999},
            "size_relaxed": True,
        }
    ]
    # Avoid Cloudinary rewrite noise
    os.environ.pop("CLOUDINARY_CLOUD_NAME", None)
    msgs = _format_products_for_whatsapp(products, "INR")
    cap = msgs[0]["caption"] if msgs else ""
    report.add("WA INR uses rupee symbol", "\u20b914,700" in cap or "Rs14,700" in cap, cap)
    report.add("WA ordinal present", cap.startswith("*1. Abrash*"), cap)
    report.add("WA size_relaxed line", "Closest available size" in cap, cap)
    return report


def suite_honesty_note() -> SuiteReport:
    report = SuiteReport("honesty")
    from qlink_chatbot.utils.product_format import format_product_search_message
    from qlink_chatbot.utils.search_session import build_search_intro, products_honesty_note

    products = [
        {
            "name": "Crimson Rect",
            "search_strategy": "drop_shape",
            "fallback_note": (
                "No rugs matched that shape with your other filters - showing other shapes."
            ),
            "size_relaxed": False,
            "display_price": "USD 18,000",
            "url": "https://www.jaipurrugs.com/in/rugs/x",
            "image": "https://example.com/x.jpg",
            "size": "8x10",
            "material": "Wool",
        }
    ]
    note = products_honesty_note(products)
    intro = build_search_intro(products=products)
    msg = format_product_search_message(products)
    report.add("single honesty note extracted", "showing other shapes" in (note or "").lower())
    report.add("intro has shape note once", intro.lower().count("showing other shapes") == 1, intro)
    report.add(
        "formatter does not invent size note",
        "closest available sizes" not in msg.lower(),
        msg[:200],
    )
    return report


def suite_kb_fixture() -> SuiteReport:
    report = SuiteReport("kb_fixture")
    path = ROOT / "data" / "policy_kb_pages.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    blob = "\n".join(f"{p.get('title','')}\n{p.get('text','')}" for p in data.get("pages", [])).lower()
    report.add("COD mentioned as not listed / caution", "cod" in blob and ("not listed" in blob or "upi" in blob))
    report.add("India return window present", "14" in blob and "return" in blob)
    report.add("rug pads / anti-slip sold", "rug pad" in blob or "anti-slip" in blob)

    from qlink_chatbot.database.pinecone_utils import kb_hits_preview_from_matches

    hits = kb_hits_preview_from_matches(
        [{"metadata": {"lable": "faq", "text": "A" * 200}}],
        max_chars=120,
    )
    preview = hits[0]["preview"] if hits else ""
    report.add(
        "KB preview truncates",
        bool(preview)
        and len(preview) == 121
        and (preview.endswith("\u2026") or preview.endswith("...")),
        repr(preview[-3:]) if preview else "empty",
    )
    return report


def suite_color_family_rank() -> SuiteReport:
    report = SuiteReport("color_family")
    from qlink_chatbot.utils.jr_search_aliases import color_search_terms
    from qlink_chatbot.utils.jr_search_recommendation import select_top_products

    soft = {
        "SKU": "PAE-5080-0001",
        "GrColor": "Soft Coral",
        "ColorFamily": "Red and Orange",
        "Shape": "Round",
        "SizeInFT": "8x10",
        "Name": "Soft",
    }
    hard = {
        "SKU": "RED-1",
        "GrColor": "Crimson Red",
        "ColorFamily": "Red",
        "Shape": "Round",
        "SizeInFT": "8x10",
        "Name": "Hard",
    }
    selected, scores = select_top_products(
        [soft, hard],
        match_terms=set(color_search_terms("red")),
        exact_color_terms={"red"},
        breakdown_by_sku={},
        color_search_tier="exact_catalog_color",
        limit=2,
    )
    report.add(
        "GrColor beats ColorFamily in rank",
        selected and selected[0]["SKU"] == "RED-1",
        f"order={[p.get('SKU') for p in selected]} scores={scores}",
    )
    return report


OFFLINE_SUITES: list[Callable[[], SuiteReport]] = [
    suite_strategies,
    suite_evidence_and_hinglish,
    suite_currency,
    suite_medium_size,
    suite_search_verdict,
    suite_wa_captions,
    suite_honesty_note,
    suite_kb_fixture,
    suite_color_family_rank,
]


def run_offline() -> int:
    _banner("OFFLINE SUITES (no Mongo / OpenAI)")
    failed = 0
    for fn in OFFLINE_SUITES:
        try:
            report = fn()
        except Exception as exc:
            _fail(f"{fn.__name__} crashed: {exc}")
            traceback.print_exc()
            failed += 1
            continue
        print(f"\n[{report.name}] {report.passed}/{len(report.results)} passed")
        for r in report.results:
            if r.ok:
                _ok(r.name)
            else:
                _fail(f"{r.name} - {r.detail}")
                failed += 1
    return failed


# ---------------------------------------------------------------------------
# Pytest runner
# ---------------------------------------------------------------------------


def run_pytest() -> int:
    _banner("PYTEST (golden / strategy / KB / WA / color)")
    tests = [
        "tests/test_jr_search_strategies.py",
        "tests/test_jr_search_golden_queries.py",
        "tests/test_kb_smoke.py",
        "tests/test_roadmap_improvements.py",
        "tests/test_whatsapp_images.py",
        "tests/test_jr_search_catalog_color.py",
        "tests/test_jr_search_catalog_tags.py",
        "tests/test_jr_search_llm_extract.py",
    ]
    existing = [t for t in tests if (ROOT / t).exists()]
    cmd = [sys.executable, "-m", "pytest", *existing, "-q", "--tb=line"]
    _info(" ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return 0 if proc.returncode == 0 else 1


# ---------------------------------------------------------------------------
# Live Mongo / bot search battery
# ---------------------------------------------------------------------------

LIVE_CASES: list[dict[str, Any]] = [
    {
        "id": "founder_red_round_usd",
        "query": "show me red rugs above 15000 usd and below 20000 usd round shape medium size",
        "currency": "USD",
        "country": "IN",
        "expect_currency": "USD",
        "notes": "Founder demo - expect rounds; PAE-5080-class if in catalog",
        "expect_sku_optional": "PAE-5080",
    },
    {
        "id": "soft_new_arrival",
        "query": "is there any new arrival",
        "currency": "INR",
        "country": "IN",
        "notes": "Must NOT bleed prior aurelia/red; strategy usually exact or drop_catalog_tag",
    },
    {
        "id": "aurelia_red",
        "query": "show me aurelia in red",
        "currency": "INR",
        "country": "IN",
        "notes": "Collection + color",
    },
    {
        "id": "hinglish_laal_gol",
        "query": "laal gol dari",
        "currency": "INR",
        "country": "IN",
        "notes": "Hinglish -> red + round",
    },
    {
        "id": "hinglish_hara",
        "query": "hara gol rugs",
        "currency": "INR",
        "country": "IN",
        "notes": "hara -> green + round",
    },
    {
        "id": "medium_5x8",
        "query": "medium size red rugs",
        "currency": "INR",
        "country": "IN",
        "notes": "Medium chip should include 5x8 / 6x9 / 8x10 class sizes",
    },
    {
        "id": "overconstrained_shape",
        "query": "red round silk medium under 5000 usd",
        "currency": "USD",
        "country": "US",
        "notes": "Likely strategy=drop_* or widen/drop_price - check honesty note",
    },
    {
        "id": "budget_only_usd",
        "query": "rugs under 2000 usd",
        "currency": "USD",
        "country": "US",
        "notes": "Currency must stay USD even if country US/IN",
    },
    {
        "id": "api_gap_needle",
        "query": None,
        "find": "laal chattan",
        "notes": "Expect API_GAP if still missing from Product Master",
    },
]


def _load_diagnose_catalog():
    import importlib.util

    path = ROOT / "scripts" / "diagnose_catalog.py"
    spec = importlib.util.spec_from_file_location("diagnose_catalog", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _run_live_query(case: dict[str, Any], *, use_llm: bool) -> CheckResult:
    diag = _load_diagnose_catalog()
    run_bot_search = diag.run_bot_search
    find_in_mongo = diag.find_in_mongo
    verdict_for_find = diag.verdict_for_find

    cid = case["id"]
    if case.get("find"):
        needle = case["find"]
        hits = find_in_mongo(needle)
        verdict = verdict_for_find(
            needle=needle,
            mongo_hits=hits,
            api_search_hits=None,
            api_master_hits=None,
        )
        ok = True  # informational - API_GAP is not a script failure
        return CheckResult(
            name=cid,
            ok=ok,
            detail=f"{verdict} mongo_hits={len(hits)}",
        )

    result = await run_bot_search(
        case["query"],
        use_llm=use_llm,
        currency=case.get("currency") or "USD",
        country=case.get("country") or "IN",
    )
    page = result.get("bot_page")
    strategy = result.get("strategy") or ""
    note = result.get("fallback_note") or ""
    skus: list[str] = []
    display_currency = ""
    if isinstance(page, list):
        for p in page:
            if isinstance(p, dict):
                if p.get("SKU"):
                    skus.append(str(p["SKU"]))
                if not display_currency:
                    display_currency = str(p.get("display_currency") or "")
        n = len(page)
        err = ""
    elif isinstance(page, dict):
        n = 0
        err = str(page.get("error") or page)
    else:
        n = 0
        err = "unexpected page type"

    expect_cur = (case.get("expect_currency") or "").upper()
    cur_ok = True
    if expect_cur and n > 0:
        cur_ok = display_currency.upper() == expect_cur

    optional = (case.get("expect_sku_optional") or "").upper()
    sku_hint = ""
    if optional:
        sku_hint = "SKU_HIT" if any(optional in s.upper() for s in skus) else "SKU_MISS"

    detail = (
        f"n={n} strategy={strategy!r} verdict_note={note[:80]!r} "
        f"currency={display_currency!r} skus={skus[:5]} {sku_hint} {err}"
    )
    # Soft new arrival: fail hard if colors/collection bleed into filters oddly -
    # we only assert we got a response path without crash.
    ok = err == "" or n > 0 or "No products" in err
    if expect_cur and n > 0 and not cur_ok:
        ok = False
        detail += " CURRENCY_MISMATCH"
    return CheckResult(name=cid, ok=ok, detail=detail + f" | {case.get('notes','')}")


async def run_live(*, use_llm: bool) -> int:
    _banner(f"LIVE BOT SEARCH BATTERY (llm={'on' if use_llm else 'off'})")
    _info("Requires Mongo + Product catalog. LLM needs OPENAI_API_KEY when --live-llm.")
    failed = 0
    for case in LIVE_CASES:
        try:
            r = await _run_live_query(case, use_llm=use_llm)
        except Exception as exc:
            r = CheckResult(name=case["id"], ok=False, detail=f"CRASH: {exc}")
            traceback.print_exc()
        if r.ok:
            _ok(f"{r.name}: {r.detail}")
        else:
            _fail(f"{r.name}: {r.detail}")
            failed += 1
    return failed


# ---------------------------------------------------------------------------
# Manual checklist
# ---------------------------------------------------------------------------


CHECKLIST = r"""
MANUAL / FOUNDER CHECKLIST
--------------------------
Web (https://qlink-jr.vercel.app or local frontend -> Vultr API):

  [ ] Open chat with ?debug=1  (drawer appears after a bot reply)
  [ ] Query: red round above 15000 usd below 20000 usd medium
        -> products in USD (not INR lakhs)
        -> drawer: strategy=...  search_verdict=exact|relaxed  SKUs listed
        -> Copy diagnose bundle works
  [ ] Query: is there any new arrival  (fresh session or after aurelia)
        -> no aurelia/red bleed; drawer strategy clear
  [ ] Query: aurelia in red
  [ ] Query: laal gol dari  /  hara gol rugs
  [ ] Policy: "what is your return policy?" / "do you accept COD?"
        -> answers from KB (COD not listed); drawer shows search_kb hits

WhatsApp:

  [ ] Vultr env has CLOUDINARY_CLOUD_NAME set
  [ ] Send product search -> cards show images + Rs prices for India
  [ ] Burst two messages -> "Still working on your previous message..."

Ops / Phase C:

  [ ] python scripts/diagnose_catalog.py --find "laal chattan" --full-api
  [ ] python scripts/diagnose_catalog.py --query "red round above 15000 usd" --expect-sku PAE-5080-0001
  [ ] If KB empty: python scripts/fast_ingest_policy_kb.py
  [ ] GET https://api.vultr3.qlink.in/ping
"""


def print_checklist() -> None:
    _banner("CHECKLIST")
    _safe_print(CHECKLIST)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extensive JR chatbot smoke suite")
    p.add_argument("--pytest", action="store_true", help="Run related pytest files")
    p.add_argument("--live", action="store_true", help="Run live Mongo bot-search battery")
    p.add_argument(
        "--live-llm",
        action="store_true",
        help="With --live, use LLM extract (needs OPENAI_API_KEY)",
    )
    p.add_argument("--checklist", action="store_true", help="Print manual checklist and exit")
    p.add_argument(
        "--skip-offline",
        action="store_true",
        help="Skip built-in offline assertion suites",
    )
    return p


def main() -> int:
    # Windows consoles often default to cp1252; keep smoke output readable.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    args = build_parser().parse_args()
    if args.checklist and not args.pytest and not args.live:
        print_checklist()
        return 0

    failures = 0
    if not args.skip_offline:
        failures += run_offline()

    if args.pytest:
        failures += run_pytest()

    if args.live:
        failures += asyncio.run(run_live(use_llm=bool(args.live_llm)))

    print_checklist()

    _banner("SUMMARY")
    if failures:
        _safe_print(f"Finished with {failures} failure(s).")
        return 1
    _safe_print("All automated checks passed (or live cases informational-OK).")
    _safe_print("Walk the MANUAL checklist above on web + WhatsApp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
