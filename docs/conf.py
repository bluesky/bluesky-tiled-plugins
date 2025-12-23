from __future__ import annotations

import importlib.metadata
from pathlib import Path
from subprocess import check_output
from typing import Any

project = "bluesky-tiled-plugins"
copyright = "Bluesky Collaboration"
author = "Bluesky Collaboration"
github_user = "bluesky"

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
    "autodoc2",
    "myst_parser",
    "numpydoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinxcontrib.mermaid",
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
html_logo = "_static/logo_bluesky.svg"
html_theme_options: dict[str, Any] = {
    "github_url": f"https://github.com/{github_user}/{project}",
    "external_links": [
        {
            "name": "Bluesky Project",
            "url": "https://blueskyproject.io",
        },
    ],
    "icon_links": [
        {
            "name": "PyPI",
            "url": f"https://pypi.org/project/{project}",
            "icon": "fas fa-cube",
        },
    ],
}
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
    "bluesky": ("https://blueskyproject.io/bluesky/main", None),
    "event_model": ("https://blueskyproject.io/event-model/main", None),
    "numpy": ("https://numpy.org/devdocs/", None),
    "python": ("https://docs.python.org/3", None),
    "tiled": ("https://blueskyproject.io/tiled", None),
}

nitpick_ignore = [
    ("py:class", "_io.StringIO"),
    ("py:class", "_io.BytesIO"),
]
# Ignore some missing intersphinx (workaround)
nitpick_ignore_regex = [
    (r"py:.*", r"event_model\..*"),
    (r"py:.*", r"tiled\..*"),
    (r"py:.*", r"bluesky_tiled_plugins._version\..*"),
    (r"py:.*", r"bluesky_tiled_plugins.clients.bluesky_run._BlueskyRunSQL"),
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

# Which package to load and document
autodoc2_packages = [{"path": "../src/bluesky_tiled_plugins"}]

# Put them in docs/_api which is git ignored
autodoc2_output_dir = "_api"

autodoc2_render_plugin = "myst"

# Don't document private things
autodoc2_hidden_objects = {"private", "dunder", "inherited"}

# We don't have any docstring for __init__, so by separating
# them here we don't get the "Initilize" text that would otherwise be added
autodoc2_class_docstring = "both"

# Which objects to include docstrings for. ‘direct’ means only from objects
# that are not inherited.
autodoc2_docstrings = "all"
