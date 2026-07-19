import { createHash } from 'node:crypto';
import {
  cp,
  lstat,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
} from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(scriptDirectory, '..');
const repositoryRoot = path.resolve(frontendRoot, '..');
const sourceDirectory = path.join(frontendRoot, 'dist');
const targetDirectory = path.join(
  repositoryRoot,
  'src',
  'okto_pulse',
  'community',
  'frontend_dist',
);
const targetParent = path.dirname(targetDirectory);
const stageDirectory = path.join(
  targetParent,
  `.frontend_dist.stage-${process.pid}`,
);
const backupDirectory = path.join(
  targetParent,
  `.frontend_dist.backup-${process.pid}`,
);

const arguments_ = new Set(process.argv.slice(2));
for (const argument of arguments_) {
  if (argument !== '--verify-only') {
    throw new Error(`Unknown argument: ${argument}`);
  }
}
const verifyOnly = arguments_.has('--verify-only');

function isInside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return (
    relative !== '' &&
    relative !== '..' &&
    !relative.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(relative)
  );
}

function assertSamePath(actual, expected, label) {
  if (path.relative(actual, expected) !== '') {
    throw new Error(`${label} resolved to an unexpected path: ${actual}`);
  }
}

function assertTemporaryPath(candidate, expectedName) {
  if (
    !isInside(targetParent, candidate) ||
    path.basename(candidate) !== expectedName
  ) {
    throw new Error(`Refusing unsafe temporary path: ${candidate}`);
  }
}

async function exists(candidate) {
  try {
    await lstat(candidate);
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

async function assertDirectory(candidate, label) {
  const details = await lstat(candidate);
  if (!details.isDirectory() || details.isSymbolicLink()) {
    throw new Error(`${label} must be a real directory: ${candidate}`);
  }
}

async function validatePaths({ requireTarget }) {
  const repositoryReal = await realpath(repositoryRoot);
  const frontendReal = await realpath(frontendRoot);
  const sourceReal = await realpath(sourceDirectory);
  const targetParentReal = await realpath(targetParent);

  assertSamePath(path.dirname(frontendReal), repositoryReal, 'Frontend parent');
  if (!isInside(frontendReal, sourceReal)) {
    throw new Error(`Build output escaped the frontend root: ${sourceReal}`);
  }
  if (!isInside(repositoryReal, targetParentReal)) {
    throw new Error(`Packaged frontend parent escaped the repository: ${targetParentReal}`);
  }
  assertSamePath(
    targetDirectory,
    path.join(
      repositoryReal,
      'src',
      'okto_pulse',
      'community',
      'frontend_dist',
    ),
    'Packaged frontend',
  );

  await assertDirectory(sourceDirectory, 'Build output');
  if (requireTarget || (await exists(targetDirectory))) {
    await assertDirectory(targetDirectory, 'Packaged frontend');
  }

  assertTemporaryPath(stageDirectory, `.frontend_dist.stage-${process.pid}`);
  assertTemporaryPath(backupDirectory, `.frontend_dist.backup-${process.pid}`);
}

async function createManifest(root) {
  const files = [];

  async function walk(directory, relativeDirectory = '') {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name));

    for (const entry of entries) {
      const absolutePath = path.join(directory, entry.name);
      const relativePath = path
        .join(relativeDirectory, entry.name)
        .split(path.sep)
        .join('/');

      if (entry.isSymbolicLink()) {
        throw new Error(`Symbolic links are forbidden in frontend bundles: ${relativePath}`);
      }
      if (entry.isDirectory()) {
        await walk(absolutePath, relativePath);
        continue;
      }
      if (!entry.isFile()) {
        throw new Error(`Unsupported bundle entry: ${relativePath}`);
      }

      const content = await readFile(absolutePath);
      files.push({
        path: relativePath,
        size: content.byteLength,
        sha256: createHash('sha256').update(content).digest('hex'),
      });
    }
  }

  await walk(root);
  files.sort((left, right) => left.path.localeCompare(right.path));
  return files;
}

function manifestDigest(manifest) {
  const hash = createHash('sha256');
  for (const file of manifest) {
    hash.update(`${file.path}\0${file.size}\0${file.sha256}\n`);
  }
  return hash.digest('hex');
}

function assertMatchingManifests(source, candidate, label) {
  if (source.length !== candidate.length) {
    throw new Error(
      `${label} inventory differs: ${candidate.length} files, expected ${source.length}`,
    );
  }
  for (let index = 0; index < source.length; index += 1) {
    const expected = source[index];
    const actual = candidate[index];
    if (
      expected.path !== actual.path ||
      expected.size !== actual.size ||
      expected.sha256 !== actual.sha256
    ) {
      throw new Error(
        `${label} differs at ${expected.path}: expected ${expected.sha256}, ` +
          `received ${actual.path}:${actual.sha256}`,
      );
    }
  }
}

await validatePaths({ requireTarget: verifyOnly });
const sourceManifest = await createManifest(sourceDirectory);
if (sourceManifest.length === 0) {
  throw new Error(`Build output is empty: ${sourceDirectory}`);
}

if (verifyOnly) {
  const targetManifest = await createManifest(targetDirectory);
  assertMatchingManifests(sourceManifest, targetManifest, 'Packaged frontend');
  console.log(
    `Verified ${targetManifest.length} packaged frontend files ` +
      `(tree sha256: ${manifestDigest(targetManifest)}).`,
  );
} else {
  await rm(stageDirectory, { recursive: true, force: true });
  await rm(backupDirectory, { recursive: true, force: true });
  let targetMoved = false;
  let stagePromoted = false;
  try {
    await cp(sourceDirectory, stageDirectory, {
      recursive: true,
      errorOnExist: true,
      force: false,
    });

    const sourceAfterCopy = await createManifest(sourceDirectory);
    assertMatchingManifests(sourceManifest, sourceAfterCopy, 'Build output snapshot');
    const stageManifest = await createManifest(stageDirectory);
    assertMatchingManifests(sourceManifest, stageManifest, 'Staged frontend');

    const hadTarget = await exists(targetDirectory);
    if (hadTarget) {
      await rename(targetDirectory, backupDirectory);
      targetMoved = true;
    }
    await rename(stageDirectory, targetDirectory);
    stagePromoted = true;

    const targetManifest = await createManifest(targetDirectory);
    assertMatchingManifests(sourceManifest, targetManifest, 'Packaged frontend');
    if (targetMoved) {
      await rm(backupDirectory, { recursive: true });
    }
    console.log(
      `Synced ${targetManifest.length} packaged frontend files ` +
        `(tree sha256: ${manifestDigest(targetManifest)}).`,
    );
  } catch (error) {
    try {
      if (stagePromoted && (await exists(targetDirectory))) {
        await rename(targetDirectory, stageDirectory);
      }
      if (targetMoved && (await exists(backupDirectory))) {
        await rename(backupDirectory, targetDirectory);
      }
    } catch (restoreError) {
      throw new AggregateError(
        [error, restoreError],
        'Frontend synchronization failed and the previous bundle could not be restored',
      );
    }
    throw error;
  } finally {
    await rm(stageDirectory, { recursive: true, force: true });
  }
}
