// Full-page screenshots of the built site: pages x languages x light/dark x desktop/mobile, plus a log of every
// console error, CSP violation, failed request and any request to another origin (there must be none).
//
//   node tools/screenshots.js http://127.0.0.1:18741 out-dir [chrome-path]
//
// Needs playwright-core. Chrome: the third argument, or CHROME_PATH, or Playwright's own download.
const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const base = process.argv[2] || "http://127.0.0.1:18741";
const out = process.argv[3] || "screenshots";
const exe = process.argv[4] || process.env.CHROME_PATH || undefined;
const PAGES = (process.env.PAGES || "home:/,how-it-works:/how-it-works/,download:/download/").split(",").map((s) => s.split(":"));
// LANGS="en:,no:/no,de:/de" (code:prefix): the languages to shoot; default English and Norwegian.
const LANGS = (process.env.LANGS || "en:,no:/no").split(",").map((s) => s.split(":"));
const SCHEMES = ["light", "dark"];
const VIEWPORTS = [
  ["desktop", { width: 1440, height: 900 }, 1, false],
  ["mobile", { width: 390, height: 844 }, 2, true],
];

(async () => {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ executablePath: exe });
  const origin = new URL(base).origin;
  const problems = [];
  for (const [vpName, viewport, dsf, mobile] of VIEWPORTS) {
    for (const scheme of SCHEMES) {
      const ctx = await browser.newContext({ viewport, deviceScaleFactor: dsf, isMobile: mobile, hasTouch: mobile,
                                             colorScheme: scheme, locale: "en-GB" });
      for (const [lang, prefix] of LANGS) {
        for (const [name, url] of PAGES) {
          const page = await ctx.newPage();
          const tag = `${name}-${lang}-${scheme}-${vpName}`;
          page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") problems.push(`${tag}: console ${m.type()}: ${m.text()}`); });
          page.on("pageerror", (e) => problems.push(`${tag}: page error: ${e.message}`));
          page.on("requestfailed", (r) => problems.push(`${tag}: failed ${r.url()} ${r.failure() && r.failure().errorText}`));
          page.on("request", (r) => { if (!r.url().startsWith(origin) && !r.url().startsWith("data:")) problems.push(`${tag}: THIRD-PARTY ${r.url()}`); });
          page.on("response", (r) => { if (r.status() >= 400) problems.push(`${tag}: HTTP ${r.status()} ${r.url()}`); });
          await page.goto(base + prefix + url, { waitUntil: "networkidle" });
          await page.evaluate(() => document.fonts.ready);
          // load lazy images before the full-page shot
          await page.evaluate(async () => {
            for (const img of document.querySelectorAll('img[loading="lazy"]')) img.loading = "eager";
            await Promise.all([...document.images].map((i) => i.complete ? 0 : new Promise((r) => { i.onload = i.onerror = r; })));
          });
          const overflow = await page.evaluate((w) => document.documentElement.scrollWidth - w, viewport.width);
          if (overflow > 0) problems.push(`${tag}: horizontal overflow ${overflow}px`);
          await page.screenshot({ path: path.join(out, `${tag}.png`), fullPage: true });
          console.log("shot", tag, await page.title());
          await page.close();
        }
      }
      await ctx.close();
    }
  }
  await browser.close();
  fs.writeFileSync(path.join(out, "problems.txt"), problems.join("\n") + "\n");
  console.log(problems.length ? `${problems.length} problems:\n` + problems.join("\n") : "no problems");
})();
