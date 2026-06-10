"""
Comprehensive test suite — Jaipur Rugs product search + chat agent.

Product-search tests call jaipur_rugs_product_search() directly.
Agent tests call chat_agent() so the full LLM + tool pipeline runs.

Run:
    python test_search.py              # all tests
    python test_search.py search       # product search only
    python test_search.py agent        # agent only
"""
import asyncio
import io
import logging
import os
import sys
import textwrap
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Logs go to logs/test.log — keep stdout clean for test summary
os.makedirs(ROOT / "logs", exist_ok=True)
os.environ.setdefault("LOG_DIR", str(ROOT / "logs"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from qlink_chatbot.utils.jaipur_rugs_api import jaipur_rugs_product_search
from qlink_chatbot.agent.chat_agent import chat_agent

# Remove stream (stdout) handler from singleton logger — file handler stays
_sl = logging.getLogger("SingletonLogger")
for _h in _sl.handlers[:]:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _sl.removeHandler(_h)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEP = "-" * 70


def banner(title: str):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def result_summary(results) -> str:
    if isinstance(results, dict) and "error" in results:
        return f"ERROR: {results['error']}"
    if not results:
        return "No results"
    lines = [f"{len(results)} product(s):"]
    for p in results:
        lines.append(
            f"  • {p['name'] or p['collection']} | SKU={p['SKU']} "
            f"| size={p['size']} | {p['display_price']} | color={p['color']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Product search cases — keyword is what the AI model would generate
# ---------------------------------------------------------------------------

SEARCH_CASES = [
    # (label, keyword, country_code, currency)

    # ── Basic ──────────────────────────────────────────────────────────────
    ("Basic: red rugs",                     "red",                          "91", ""),
    ("Basic: blue carpet",                  "blue",                         "91", ""),
    ("Basic: ivory rugs",                   "ivory",                        "91", ""),
    ("Basic: 8x10 size",                    "8x10",                         "91", ""),
    ("Basic: wool rug",                     "wool",                         "91", ""),
    ("Basic: hand knotted",                 "hand knotted",                 "91", ""),
    ("Basic: modern rug",                   "modern",                       "91", ""),

    # ── Multi-filter ───────────────────────────────────────────────────────
    ("Multi: red wool 8x10",                "red&wool&8x10",                "91", ""),
    ("Multi: blue hand knotted <INR 50000", "blue&hand knotted&under INR 50000", "91", ""),
    ("Multi: ivory traditional 9x12",       "ivory&traditional&9x12",       "91", ""),
    ("Multi: silk >USD 1000",               "silk&above USD 1000",          "1",  "USD"),
    ("Multi: geometric 6x9 <INR30000",        "geometric&6x9&under INR 30000","91", ""),

    # ── Price filters ──────────────────────────────────────────────────────
    ("Price: under 50000 rupees",           "under INR 50000",              "91", ""),
    ("Price: above 2 lakh",                 "above INR 200000",             "91", ""),
    ("Price: budget 80000 INR",             "INR 80000",                    "91", ""),
    ("Price: between 30000 and 80000",      "between INR 30000 and 80000",  "91", ""),
    ("Price: under $500",                   "under USD 500",                "1",  "USD"),
    ("Price: above AED 2000",               "above AED 2000",               "971","AED"),

    # ── Color edge cases ───────────────────────────────────────────────────
    ("Color: multicolor",                   "multicolor",                   "91", ""),
    ("Color: rust",                         "rust",                         "91", ""),
    ("Color: terracotta",                   "terracotta",                   "91", ""),
    ("Color: olive OR sage",                "olive||sage",                  "91", ""),
    ("Color: navy blue",                    "navy",                         "91", ""),
    ("Color: red AND gold",                 "red&gold",                     "91", ""),
    ("Color: pink rugs",                    "pink",                         "91", ""),
    ("Color: pink (natural language)",      "Show me pink rugs",            "91", ""),
    ("Pattern: solid beige",                "show me solid beige rugs",     "91", ""),

    # ── Edge / stress ──────────────────────────────────────────────────────
    ("Edge: under INR500 (no results?)",      "under INR 500",                "91", ""),
    ("Edge: size 347x289 (unusual)",        "347x289",                      "91", ""),
    ("Edge: dark (vague)",                  "dark",                         "91", ""),
    ("Edge: off-white (not in list)",       "off-white",                    "91", ""),
]


# ---------------------------------------------------------------------------
# Agent cases — full LLM + tool pipeline
# ---------------------------------------------------------------------------

AGENT_CASES = [
    # (category, user_message, country_code)

    # Greetings
    ("Greeting",  "Hi",                                                "91"),
    ("Greeting",  "Hello, I need help",                                "91"),

    # Product — natural language (AI extracts keyword internally)
    ("Product",   "Show me red rugs",                                  "91"),
    ("Product",   "Show me pink rugs",                                 "91"),
    ("Product",   "I need a blue carpet for my living room",           "91"),
    ("Product",   "Do you have ivory rugs?",                           "91"),
    ("Product",   "Show me rugs in size 8x10",                         "91"),
    ("Product",   "I want a wool rug",                                  "91"),
    ("Product",   "Show me hand knotted rugs",                         "91"),
    ("Product",   "I need a modern rug",                               "91"),
    ("Product",   "Show me red wool rugs in 8x10",                     "91"),
    ("Product",   "I want a blue hand knotted rug under INR 50000",    "91"),
    ("Product",   "Do you have ivory traditional rugs in 9x12?",       "91"),
    ("Product",   "Show me silk rugs above USD 1000",                  "1"),
    ("Product",   "I need a geometric rug in 6x9 under INR30000",        "91"),
    ("Product",   "Show me rugs under 50000 rupees",                   "91"),
    ("Product",   "I want rugs above 2 lakh",                          "91"),
    ("Product",   "Budget is 80000 INR, what do you have?",            "91"),
    ("Product",   "Show me rugs between 30000 and 80000",              "91"),
    ("Product",   "Do you have rugs under $500?",                      "1"),
    ("Product",   "Show me rugs above AED 2000",                       "971"),
    ("Product",   "I want a multicolor rug",                           "91"),
    ("Product",   "Do you have rust colored rugs?",                    "91"),
    ("Product",   "Show me terracotta rugs",                           "91"),
    ("Product",   "I want something in olive or sage",                 "91"),
    ("Product",   "Show me navy blue rugs",                            "91"),
    ("Product",   "I want red and gold rugs",                          "91"),
    ("Product",   "Do you have off-white rugs?",                       "91"),
    ("Product",   "Show me dark rugs",                                 "91"),
    ("Product",   "I want the cheapest rug you have",                  "91"),
    ("Product",   "Show me your best rug",                             "91"),

    # Store & location
    ("Store",     "Where is your nearest store?",                      "91"),
    ("Store",     "Do you have a showroom in Delhi?",                  "91"),
    ("Store",     "What are your store timings?",                      "91"),
    ("Store",     "Is there a Jaipur Rugs store in Mumbai?",           "91"),
    ("Store",     "Do you have stores outside India?",                 "91"),
    ("Store",     "Where can I see rugs in person in Bangalore?",      "91"),

    # Contact & support
    ("Contact",   "How can I contact you?",                            "91"),
    ("Contact",   "I need to speak to someone",                        "91"),
    ("Contact",   "Give me your email",                                "91"),
    ("Contact",   "What is your WhatsApp number?",                     "91"),
    ("Contact",   "I want to talk to a human agent",                   "91"),
    ("Contact",   "Can I call you?",                                   "91"),

    # Orders & shipping
    ("Orders",    "Where is my order?",                                "91"),
    ("Orders",    "How do I track my delivery?",                       "91"),
    ("Orders",    "How long does shipping take?",                      "91"),
    ("Orders",    "Do you ship internationally?",                      "91"),
    ("Orders",    "What are your return/exchange policies?",           "91"),
    ("Orders",    "I placed an order last week, no update",            "91"),

    # Special requests
    ("Special",   "I want a bulk order of 20 rugs",                    "91"),
    ("Special",   "Do you give discounts on bulk orders?",             "91"),
    ("Special",   "I need a custom rug with my own design",            "91"),
    ("Special",   "Can I get a personalised rug?",                     "91"),
    ("Special",   "Do you do corporate orders?",                       "91"),

    # Cleaning & care
    ("Cleaning",  "Do you clean rugs?",                                "91"),
    ("Cleaning",  "How do I clean a wool rug at home?",                "91"),
    ("Cleaning",  "Do you clean rugs from other brands?",              "91"),
    ("Cleaning",  "How much does rug cleaning cost?",                  "91"),
    ("Cleaning",  "My rug has a stain, what do I do?",                 "91"),

    # Careers
    ("Careers",   "Are you hiring?",                                   "91"),
    ("Careers",   "I want to apply for a job",                         "91"),
    ("Careers",   "Do you have internships?",                          "91"),

    # Edge / stress
    ("Edge",      "What is the capital of France?",                    "91"),
    ("Edge",      "Who are you?",                                      "91"),
    ("Edge",      "Are you a bot or human?",                           "91"),
    ("Edge",      "Do you have rugs for a 10x10 room?",                "91"),
    ("Edge",      "I want a rug that goes with grey sofa",             "91"),
    ("Edge",      "My rug is 3 years old, can I exchange it?",         "91"),
    ("Edge",      "I bought from you online, wrong item delivered",    "91"),
    ("Edge",      "Show me something under INR500",                      "91"),
    ("Edge",      "I want a rug exactly 347x289 cm",                   "91"),
    ("Edge",      "हिंदी में बात करें",                                "91"),
]


# ---------------------------------------------------------------------------
# Follow-up tests (simulate chat history with prior product results)
# ---------------------------------------------------------------------------

DUMMY_PRODUCTS_IN_HISTORY = [
    {"role": "user", "content": "Show me red rugs"},
    {
        "role": "assistant",
        "content": (
            "Here are some red rugs:\n"
            "1. **Vintage** SKU: PAE-2816-0001 | Size: 5x9 | Price: INR 85,050 | "
            "url: https://www.jaipurrugs.com/in/rugs/pae-2816-antique-red-rug?barcode=RUG1138001\n"
            "2. **Manifest** SKU: AKWL-1561-0002 | Size: 8x10 | Price: INR 98,700 | "
            "url: https://www.jaipurrugs.com/in/rugs/akwl-1561-copper-copper-rug?barcode=RUG1159919\n"
            "3. **Chaos Theory** SKU: ESKN-1004-0001 | Size: 9x12 | Price: INR 402,570\n"
        ),
    },
]

FOLLOWUP_CASES = [
    ("Follow-up: price of first",       "What is the price of the first one?"),
    ("Follow-up: different size",       "Do you have it in a different size?"),
    ("Follow-up: show more like these", "Show me more like these"),
    ("Follow-up: material",             "What is it made of?"),
    ("Follow-up: get link",             "Can I get the link?"),
    ("Follow-up: show more (paginate)", "Show me more rugs"),
    ("Follow-up: price in dollars",     "Price in dollars?"),
]


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

async def run_search_tests():
    banner("PRODUCT SEARCH TESTS (direct API calls)")
    passed = failed = 0

    for label, keyword, country_code, currency in SEARCH_CASES:
        print(f"\n{SEP}")
        print(f"TEST : {label}")
        print(f"  keyword={keyword!r}  country={country_code}  currency={currency!r}")
        try:
            results = await jaipur_rugs_product_search(
                keyword,
                country_code=country_code,
                requested_currency=currency,
            )
            summary = result_summary(results)
            print(f"  RESULT: {summary}")
            if isinstance(results, dict) and "error" in results:
                failed += 1
            else:
                passed += 1
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            failed += 1

    print(f"\n{'=' * 70}")
    print(f"Search tests done — passed: {passed}  failed/no-result: {failed}")


async def run_agent_tests():
    banner("AGENT TESTS (full LLM + tool pipeline)")
    passed = failed = 0

    for category, message, country_code in AGENT_CASES:
        print(f"\n{SEP}")
        print(f"[{category}] Q: {message!r}")
        try:
            reply = await chat_agent(
                chat_history=[],
                user_message=message,
                session_id=f"test_{abs(hash(message)) % 100000:05d}",
                country_code=country_code,
            )
            wrapped = textwrap.fill(str(reply), width=90, subsequent_indent="       ")
            print(f"  A: {wrapped}")
            passed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'=' * 70}")
    print(f"Agent tests done — passed: {passed}  errors: {failed}")


async def run_followup_tests():
    banner("FOLLOW-UP TESTS (with simulated chat history)")

    for label, message in FOLLOWUP_CASES:
        print(f"\n{SEP}")
        print(f"TEST : {label}")
        print(f"  Q: {message!r}")
        try:
            reply = await chat_agent(
                chat_history=DUMMY_PRODUCTS_IN_HISTORY,
                user_message=message,
                session_id="test_followup",
                country_code="91",
            )
            wrapped = textwrap.fill(str(reply), width=90, subsequent_indent="       ")
            print(f"  A: {wrapped}")
        except Exception as e:
            print(f"  ERROR: {e}")


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("all", "search"):
        await run_search_tests()
    if mode in ("all", "agent"):
        await run_agent_tests()
    if mode in ("all", "followup"):
        await run_followup_tests()


if __name__ == "__main__":
    asyncio.run(main())
