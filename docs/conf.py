from __future__ import annotations

import importlib.metadata
from pathlib import Path
from subprocess import check_output
from typing import Any

project = "bluesky-tiled-plugins"
copyright = "Bluesky Collaboration"
author = "Bluesky Collaboration"

# The full version, including alpha/beta/rc tags.
release = importlib.metadata.version("bluesky_tiled_plugins")

# The short X.Y version.
if "+" in release:
    # Not on a tag, use branch name
    root = Path(__file__).absolute().parent.parent
    git_branch = check_output("git branch --show-current".split(), cwd=root)
    version = release = git_branch.decode().strip()
else:
    version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinxcontrib.mermaid",
    "sphinx.ext.napoleon",
    "sphinx_autodoc_typehints",
    "sphinx_copybutton",
    "sphinx_design",
]

source_suffix = [".rst", ".md"]
exclude_patterns = [
    "_build",
    "**.ipynb_checkpoints",
    "Thumbs.db",
    ".DS_Store",
    ".env",
    ".venv",
]

html_theme = "pydata_sphinx_theme"

html_theme_options: dict[str, Any] = {
    "github_url": "https://github.com/bluesky/bluesky-tiled-plugins",
    "external_links": [
        {
            "name": "Bluesky Project",
            "url": "https://blueskyproject.io",
        },
    ],
}
github_user = "bluesky"
html_context = {
    "github_user": github_user,
    "github_repo": project,
    "github_version": version,
    "doc_path": "docs",
}

myst_enable_extensions = [
    "colon_fence",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

nitpick_ignore = [
    ("py:class", "_io.StringIO"),
    ("py:class", "_io.BytesIO"),
]

always_document_param_types = True

# If true, "Created using Sphinx" is shown in the HTML footer. Default is True.
html_show_sphinx = False

# If true, "(C) Copyright ..." is shown in the HTML footer. Default is True.
html_show_copyright = False

# Set copy-button to ignore python and bash prompts
# https://sphinx-copybutton.readthedocs.io/en/latest/use.html#using-regexp-prompt-identifiers
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: "
copybutton_prompt_is_regexp = True
