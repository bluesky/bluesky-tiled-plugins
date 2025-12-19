docs:
  rm -rf docs/_build && rm -rf docs/_api && uv run --group docs sphinx-autobuild -nT docs docs/_build/html
