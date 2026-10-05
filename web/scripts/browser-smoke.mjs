import { chromium } from "@playwright/test";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(process.env.ENERGY_BROWSER_URL || "http://127.0.0.1:18081");
await page
  .locator("input[type=password]")
  .fill(process.env.ENERGY_BROWSER_PASSWORD || "synthetic-demo-password");
await page.getByRole("button", { name: "Sign in", exact: true }).click();
await page.getByRole("tab", { name: "Overview", exact: true }).waitFor();
await page.screenshot({
  path: "/tmp/energy-overview-desktop.png",
  fullPage: true,
});
for (const name of [
  "Forecasts",
  "Setup",
  "Tariff",
  "Calendars",
  "Data quality",
  "Models",
  "Calibration",
  "Battery",
  "Plan details",
  "Administration",
]) {
  await page.getByRole("tab", { name, exact: true }).click();
  await page.getByRole("heading", { name, exact: true }).waitFor();
  if (name === "Tariff") {
    await page.getByRole("button", { name: "Preview saved rules" }).click();
    await page.locator("pre").waitFor();
  }
  if (name === "Forecasts") {
    await page.getByRole("button", { name: "24 hours", exact: true }).click();
    await page.getByRole("button", { name: "48 hours", exact: true }).click();
  }
}
await page.getByRole("tab", { name: "Overview", exact: true }).click();
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({
  path: "/tmp/energy-overview-mobile.png",
  fullPage: true,
});
await page.waitForFunction(
  () => document.documentElement.scrollWidth <= window.innerWidth,
);
await page.screenshot({
  path: "/tmp/energy-overview-mobile.png",
  fullPage: true,
});
const overflow = await page.evaluate(
  () => document.documentElement.scrollWidth > window.innerWidth,
);
console.log(
  JSON.stringify({
    pageErrors: errors,
    mobileHorizontalOverflow: overflow,
    pagesChecked: 11,
  }),
);
await browser.close();
if (errors.length || overflow) process.exit(1);
