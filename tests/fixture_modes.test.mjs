import assert from 'node:assert/strict';
import test from 'node:test';

import { hasUnsafeWritableParentMode } from '../tools/fixture_modes.mjs';

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
