"""Resolve optional browser prerequisites consistently across panel regressions."""
import os
import shutil
import subprocess


def browser_node(test):
    """Return the Node executable or skip when Node, Playwright or Chromium is absent."""
    node = shutil.which(os.environ.get('NODE_BINARY') or 'node')
    if not node:
        test.skipTest('Node required')
    probe = subprocess.run([node, '-e',
                            "const p = require(process.env.PAYMENTER_BACKUPS_PLAYWRIGHT || 'playwright'); "
                            "if (!require('node:fs').existsSync(p.chromium.executablePath())) process.exit(1)"],
                           capture_output=True, text=True, check=False)
    if probe.returncode:
        test.skipTest('Playwright and its Chromium browser required')
    return node
