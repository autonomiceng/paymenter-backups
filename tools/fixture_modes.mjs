import { realpath, stat } from 'node:fs/promises';
import path from 'node:path';

export function hasUnsafeWritableParentMode(mode) {
  return Boolean(mode & 0o022) && !(mode & 0o1000);
}

export async function hasSafeTemporaryAncestors(directory) {
  try {
    let current = await realpath(directory);
    while (true) {
      const info = await stat(current);
      if (!info.isDirectory() || ![0, process.geteuid()].includes(info.uid)
          || hasUnsafeWritableParentMode(info.mode)) return false;
      const parent = path.dirname(current);
      if (parent === current) return true;
      current = parent;
    }
  } catch {
    return false;
  }
}
