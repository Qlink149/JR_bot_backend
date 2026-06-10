/**
 * Scrape all URLs linked from https://www.jaipurrugs.com/in/sitemap
 * Uses Playwright to pass Cloudflare and render JS-driven links.
 *
 * Usage:
 *   node scripts/scrape_jaipurrugs_sitemap.mjs [--out data/sitemap_scrape.json]
 */
import { chromium } from "playwright";
import { writeFileSync, mkdirSync } from "fs";
import { dirname, resolve } from "path";

const SITEMAP_URL = "https://www.jaipurrugs.com/in/sitemap";
const BASE_HOST = "www.jaipurrugs.com";

const SKIP_PATH_RE =
  /\/(?:cart|checkout|login|registration|profile|password|payment|thankyou|track|order|favourite|cancel|adm\/|cdn-cgi\/|search\?|ajax|autoLogout|email-protection|change-password|designer-login|logout|my-profile)/i;

const SKIP_EXT_RE = /\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|css|js)$/i;

const CF_MARKERS = [
  "performing security verification",
  "just a moment",
  "enable javascript and cookies",
  "waiting for www.jaipurrugs.com to respond",
];

function normalizeUrl(href, pageUrl) {
  try {
    const url = new URL(href, pageUrl);
    if (url.hostname !== BASE_HOST && url.hostname !== "jaipurrugs.com") return null;
    url.hash = "";
    if (!url.pathname.startsWith("/in/") && url.pathname !== "/in") return null;
    if (SKIP_PATH_RE.test(url.pathname + url.search)) return null;
    if (SKIP_EXT_RE.test(url.pathname)) return null;
    url.search = "";
    let path = url.pathname.replace(/\/+$/, "") || "/in";
    url.pathname = path;
    return url.toString();
  } catch {
    return null;
  }
}

function cleanText(text) {
  return (text || "")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

function isCloudflareShell(title, text) {
  const blob = `${title || ""} ${text || ""}`.toLowerCase();
  return CF_MARKERS.some((m) => blob.includes(m));
}

async function waitForRealPage(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 120000 });

  for (let attempt = 0; attempt < 12; attempt++) {
    const title = await page.title();
    const snippet = await page.evaluate(() => (document.body?.innerText || "").slice(0, 500));
    if (!isCloudflareShell(title, snippet)) {
      await page.waitForTimeout(1200);
      return true;
    }
    await page.waitForTimeout(2500);
  }
  return false;
}

async function extractLinks(page) {
  return page.evaluate(() => {
    const out = new Set();
    for (const a of document.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href");
      if (href) out.add(href);
    }
    return [...out];
  });
}

async function extractPageContent(page) {
  return page.evaluate(() => {
    const title = document.title || "";
    const h1 = document.querySelector("h1")?.innerText?.trim() || "";
    const root =
      document.querySelector("main") ||
      document.querySelector('[role="main"]') ||
      document.querySelector("#content") ||
      document.querySelector(".page-content") ||
      document.body;
    const clone = root.cloneNode(true);
    for (const sel of ["script", "style", "nav", "footer", "header", "noscript", "iframe"]) {
      clone.querySelectorAll(sel).forEach((el) => el.remove());
    }
    const text = clone.innerText || "";
    return { title, h1, text };
  });
}

async function main() {
  const outArg = process.argv.find((a) => a.startsWith("--out="));
  const outPath = resolve(
    outArg ? outArg.split("=")[1] : "data/sitemap_scrape.json"
  );

  console.log(`[scrape] launching browser for ${SITEMAP_URL}`);
  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-blink-features=AutomationControlled"],
  });
  const context = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    locale: "en-IN",
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  const sitemapReady = await waitForRealPage(page, SITEMAP_URL);
  if (!sitemapReady) {
    throw new Error("Could not pass Cloudflare on sitemap page");
  }
  await page.waitForLoadState("networkidle", { timeout: 60000 }).catch(() => {});

  const rawLinks = await extractLinks(page);
  const urlSet = new Set();
  for (const href of rawLinks) {
    const normalized = normalizeUrl(href, SITEMAP_URL);
    if (normalized) urlSet.add(normalized);
  }
  urlSet.add(SITEMAP_URL);

  const urls = [...urlSet].sort();
  console.log(`[scrape] found ${urls.length} unique /in/ URLs on sitemap`);

  const results = [];
  let ok = 0;
  let failed = 0;

  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    process.stdout.write(`[scrape] ${i + 1}/${urls.length} ${url}\n`);

    try {
      const ready = await waitForRealPage(page, url);
      await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
      const { title, h1, text } = await extractPageContent(page);
      const body = cleanText(text);

      if (!ready || !body || body.length < 120 || isCloudflareShell(title, body)) {
        failed++;
        results.push({
          url,
          title: cleanText(title),
          h1: cleanText(h1),
          text: body,
          status: "blocked",
          scraped_at: new Date().toISOString(),
        });
      } else {
        ok++;
        results.push({
          url,
          title: cleanText(title),
          h1: cleanText(h1),
          text: body.slice(0, 50000),
          status: "ok",
          scraped_at: new Date().toISOString(),
        });
      }
    } catch (err) {
      failed++;
      results.push({
        url,
        title: "",
        h1: "",
        text: "",
        status: "error",
        error: String(err?.message || err),
        scraped_at: new Date().toISOString(),
      });
    }

    await page.waitForTimeout(600);
  }

  await browser.close();

  mkdirSync(dirname(outPath), { recursive: true });
  const payload = {
    sitemap_url: SITEMAP_URL,
    scraped_at: new Date().toISOString(),
    total_urls: urls.length,
    ok,
    failed,
    pages: results,
  };
  writeFileSync(outPath, JSON.stringify(payload, null, 2), "utf-8");
  console.log(`[scrape] done — ok=${ok} failed=${failed} → ${outPath}`);
}

main().catch((err) => {
  console.error("[scrape] fatal:", err);
  process.exit(1);
});
