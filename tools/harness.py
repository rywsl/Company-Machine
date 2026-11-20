"""Compatibility entry point for the existing uv verification command."""

import subprocess
import sys

raise SystemExit(subprocess.run(["bun", "run", "tools/harness.ts", *sys.argv[1:]], check=False).returncode)
