import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ESLint } from 'eslint';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(scriptDirectory, '..');
const baselinePath = path.join(frontendRoot, 'eslint-warning-baseline.json');

const baseline = JSON.parse(await readFile(baselinePath, 'utf8'));
for (const [ruleId, budget] of Object.entries(baseline)) {
  if (!Number.isInteger(budget) || budget < 0) {
    throw new Error(`Invalid warning budget for ${ruleId}: ${budget}`);
  }
}

const eslint = new ESLint({ cwd: frontendRoot });
const results = await eslint.lintFiles(['.']);
const formatter = await eslint.loadFormatter('stylish');
const formatted = formatter.format(results);
if (formatted) {
  console.log(formatted);
}

const warningCounts = new Map();
let errorCount = 0;
for (const result of results) {
  errorCount += result.errorCount;
  for (const message of result.messages) {
    if (message.severity !== 1) continue;
    const ruleId = message.ruleId ?? '<unattributed>';
    warningCounts.set(ruleId, (warningCounts.get(ruleId) ?? 0) + 1);
  }
}

const regressions = [];
for (const [ruleId, actual] of warningCounts) {
  const budget = baseline[ruleId] ?? 0;
  if (actual > budget) {
    regressions.push({ ruleId, actual, budget });
  }
}

const warningTotal = [...warningCounts.values()].reduce(
  (total, count) => total + count,
  0,
);
const baselineTotal = Object.values(baseline).reduce(
  (total, count) => total + count,
  0,
);
console.log(
  `Lint ratchet: ${warningTotal} warnings across ${warningCounts.size} rules ` +
    `(per-rule baseline total: ${baselineTotal}).`,
);

if (regressions.length > 0) {
  regressions.sort((left, right) => left.ruleId.localeCompare(right.ruleId));
  for (const { ruleId, actual, budget } of regressions) {
    console.error(`Warning budget exceeded for ${ruleId}: ${actual} > ${budget}`);
  }
}

if (errorCount > 0 || regressions.length > 0) {
  process.exitCode = 1;
}
