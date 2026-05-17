#!/usr/bin/env node
// Extract every inline <script> block from index.html and syntax-check it.
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';

const html = readFileSync('index.html', 'utf8');
const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
const tmp = mkdtempSync(join(tmpdir(), 'jscheck-'));
let m, idx = 0, failed = 0;

while ((m = re.exec(html)) !== null) {
  const attrs = m[1] || '';
  if (/\bsrc\s*=/.test(attrs)) continue;
  if (/type\s*=\s*["'](?!text\/javascript|module|application\/javascript)/i.test(attrs)) continue;
  const startLine = html.slice(0, m.index).split('\n').length;
  const path = join(tmp, `block-${idx}.js`);
  writeFileSync(path, m[2]);
  try {
    execFileSync(process.execPath, ['--check', path], { stdio: 'pipe' });
    console.log(`OK   block ${idx} (line ${startLine}, ${m[2].length} bytes)`);
  } catch (e) {
    failed++;
    console.error(`FAIL block ${idx} (line ${startLine}):\n${e.stderr?.toString() || e.message}`);
  }
  idx++;
}

if (idx === 0) console.log('No inline JS blocks found.');
if (failed > 0) {
  console.error(`\n${failed} block(s) failed syntax check.`);
  process.exit(1);
}
