#!/usr/bin/env node
// Load index.html in headless Chromium and fail on pageerrors/console errors.
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';

const candidates = [
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean);

const executablePath = candidates.find(p => existsSync(p));
if (!executablePath) {
  console.warn('runtime-check: no Chromium binary found; skipping. Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to enable.');
  process.exit(0);
}

const browser = await chromium.launch({ executablePath, args: ['--no-sandbox', '--ignore-certificate-errors'] });
const ctx = await browser.newContext();
const page = await ctx.newPage();
const pageErrors = [], consoleErrors = [], failedReqs = [];

page.on('pageerror', e => pageErrors.push(`${e.message}\n${e.stack || ''}`));
page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
page.on('requestfailed', r => failedReqs.push(`${r.url()} — ${r.failure()?.errorText}`));

const fileUrl = pathToFileURL(resolve('index.html')).href;
await page.goto(fileUrl, { waitUntil: 'load', timeout: 30000 });
await page.waitForTimeout(1500);
await browser.close();

let fail = false;
if (pageErrors.length) { fail = true; console.error('PAGE ERRORS:'); pageErrors.forEach(e => console.error(e)); }
if (consoleErrors.length) { fail = true; console.error('CONSOLE ERRORS:'); consoleErrors.forEach(e => console.error(e)); }
if (failedReqs.length) { console.warn('Failed requests (informational):'); failedReqs.forEach(e => console.warn(e)); }
if (!fail) console.log('runtime-check: OK');
process.exit(fail ? 1 : 0);
