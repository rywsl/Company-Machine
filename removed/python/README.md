# Historical Python implementation

This directory preserves the former Python control plane and its Python
acceptance and system fixtures while the active product runs on Bun and
TypeScript.

The contents are historical source and evidence. The active entry points are
the Bun scripts in the repository root and `backend/`.

The archived Python project can be inspected in `pyproject.toml` and its
source tree under `src/`. Its old test suites remain under `tests/`, with
fixtures kept at the repository root when they are shared by the Bun runtime.

Do not import from this directory in the active Bun control plane. A future
migration may remove the archive after the corresponding Bun behavior has
coverage and an explicit release decision.
