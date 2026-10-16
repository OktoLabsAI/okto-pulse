import { createHash } from 'node:crypto';
import { lstat, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const contractFile = 'pulse-frontend-contract.json';

export async function buildFrontendContract(root, releaseVersion, coreContract) {
  const files = [];
  let bytes = 0;
  async function walk(directory, relative = '') {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      if (name === contractFile) continue;
      const absolute = path.join(directory, entry.name);
      const stat = await lstat(absolute);
      if (stat.isSymbolicLink()) throw new Error('Frontend aliases are not supported');
      if (stat.isDirectory()) {
        await walk(absolute, name);
      } else if (stat.isFile()) {
        bytes += stat.size;
        if (bytes > 64 * 1024 * 1024 || files.length >= 1000) {
          throw new Error('Frontend distribution exceeds its artifact budget');
        }
        const content = await readFile(absolute);
        files.push({ path: name, size: content.length, sha256: createHash('sha256').update(content).digest('hex') });
      } else {
        throw new Error('Unsupported frontend entry');
      }
    }
  }
  await walk(root);
  if (!files.some(file => file.path === 'index.html')) throw new Error('Frontend index.html is missing');
  files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  return { format: 'pulse-frontend-distribution/v1', release_version: releaseVersion,
    core_contract: coreContract, files };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const repo = path.resolve(frontend, '..');
  const pyproject = await readFile(path.join(repo, 'pyproject.toml'), 'utf8');
  const adapter = await readFile(path.join(repo, 'src/okto_pulse/community/adapters/distribution_compatibility.py'), 'utf8');
  const version = pyproject.match(/^version\s*=\s*["']([^"']+)["']/m)?.[1];
  const core = adapter.match(/^EXPECTED_CORE_CONTRACT\s*=\s*["']([^"']+)["']/m)?.[1];
  if (!version || !core) throw new Error('Community release contract is missing');
  const root = path.join(frontend, 'dist');
  const contract = await buildFrontendContract(root, version, core);
  await writeFile(path.join(root, contractFile), `${JSON.stringify(contract, null, 2)}\n`);
  console.log(`Frontend contract: ${contract.files.length} assets, release ${version}, ${core}`);
}
