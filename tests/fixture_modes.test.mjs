import assert from 'node:assert/strict';
import { chmod, mkdtemp, mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';

import { hasSafeTemporaryAncestors, hasUnsafeWritableParentMode } from '../tools/fixture_modes.mjs';

test('sticky world-writable temporary parents are accepted', () => {
  assert.equal(hasUnsafeWritableParentMode(0o1777), false);
});

test('non-sticky world-writable temporary parents are rejected', () => {
  assert.equal(hasUnsafeWritableParentMode(0o0777), true);
});

test('owner-private temporary parents are accepted with a boolean result', () => {
  const result = hasUnsafeWritableParentMode(0o0700);
  assert.equal(result, false);
  assert.equal(typeof result, 'boolean');
});

test('rejects a private temporary directory below a group-writable ancestor', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'backups-temp-ancestor-'));
  try {
    const ancestor = path.join(root, 'replaceable');
    const temporary = path.join(ancestor, 'private-tmp');
    await mkdir(temporary, {recursive: true, mode: 0o700});
    await chmod(ancestor, 0o700);
    assert.equal(await hasSafeTemporaryAncestors(temporary), true);
    await chmod(ancestor, 0o770);
    assert.equal(await hasSafeTemporaryAncestors(temporary), false);
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
