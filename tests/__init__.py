"""Project test package.

Keeping the repository tests as an explicit package makes ``uv run pytest``
resolve local ``tests.*`` imports before unrelated installed packages on
Windows environments.
"""
