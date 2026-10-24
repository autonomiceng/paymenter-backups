export function hasUnsafeWritableParentMode(mode) {
  return Boolean(mode & 0o022) && !(mode & 0o1000);
}
