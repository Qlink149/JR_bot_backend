"""Quick regression: LLM extraction flag off leaves keywords unchanged for strong inputs."""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


async def main():
    mode = os.getenv("SEARCH_EXTRACTION_MODE", "regex")
    print(f"SEARCH_EXTRACTION_MODE={mode}")

    import importlib
    import qlink_chatbot.utils.jr_search_llm_extract as extract_mod
    import qlink_chatbot.utils.jr_search_currency as currency_mod
    importlib.reload(currency_mod)
    importlib.reload(extract_mod)

    from qlink_chatbot.utils.jr_search_currency import extract_price_filter_from_text
    from qlink_chatbot.utils.jr_search_llm_extract import resolve_keyword_with_llm_extraction

    pf, _ = extract_price_filter_from_text("above 50k usd")
    ok = pf and pf.get("amount") == 50_000
    print(f"  50k parse: {pf} {'PASS' if ok else 'FAIL'}")

    cases = [
        ("red", "red"),
        ("blue&round", "blue&round"),
        ("under INR 50000", "under INR 50000"),
    ]
    for inp, _ in cases:
        out, dbg = await resolve_keyword_with_llm_extraction(inp, extraction_mode="regex")
        passed = out == inp and dbg and dbg.get("reason") == "mode_regex"
        print(f"  {inp!r} -> {out!r} {'PASS' if passed else 'FAIL'}")
        ok = ok and passed

    vague, dbg = await resolve_keyword_with_llm_extraction(
        "show me something nice",
        extraction_mode="regex",
    )
    passed = vague == "show me something nice"
    print(f"  vague unchanged (regex mode): {passed}")
    ok = ok and passed

    print("REGRESSION:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
