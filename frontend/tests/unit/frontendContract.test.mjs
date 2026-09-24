// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { buildFrontendContract, contractFile } from '../../scripts/write-frontend-contract.mjs';

const directories = [];
async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'pulse-frontend-contract-'));
  directories.push(root);
  await mkdir(path.join(root, 'assets'));
  await writeFile(path.join(root, 'index.html'), '<html>Pulse</html>');
  await writeFile(path.join(root, 'assets/app.js'), 'export const edition = "Pulse";');
  return root;
}
afterEach(async () => {
  for (const directory of directories.splice(0)) {
    if (!path.basename(directory).startsWith('pulse-frontend-contract-')) throw new Error('Unsafe fixture path');
    await rm(directory, { recursive: true, force: true });
  }
});

describe('distributed frontend contract', () => {
  it('records the release and exact assets without including its own previous manifest', async () => {
    const root = await fixture();
    await writeFile(path.join(root, contractFile), 'old manifest');
    const contract = await buildFrontendContract(root, '0.3.4', 'pulse-edition-api/4');
    expect(contract.release_version).toBe('0.3.4');
    expect(contract.core_contract).toBe('pulse-edition-api/4');
    expect(contract.files.map(file => file.path)).toEqual(['assets/app.js', 'index.html']);
    for (const file of contract.files) {
      const content = await readFile(path.join(root, file.path));
      expect(file.size).toBe(content.length);
      expect(file.sha256).toBe(createHash('sha256').update(content).digest('hex'));
    }
  });
  it('changes the asset evidence when compiled content changes', async () => {
    const root = await fixture();
    const before = await buildFrontendContract(root, '0.3.4', 'pulse-edition-api/4');
    await writeFile(path.join(root, 'assets/app.js'), 'incompatible compiled app');
    const after = await buildFrontendContract(root, '0.3.4', 'pulse-edition-api/4');
    expect(after.files[0].sha256).not.toBe(before.files[0].sha256);
    expect(after.files[1]).toEqual(before.files[1]);
  });
  it('refuses a bundle without its entry point', async () => {
    const root = await fixture();
    await rm(path.join(root, 'index.html'));
    await expect(buildFrontendContract(root, '0.3.4', 'pulse-edition-api/4')).rejects.toThrow('index.html');
  });
});
