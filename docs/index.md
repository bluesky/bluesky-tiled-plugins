---
html_theme.sidebar_secondary.remove: true
---

```{include} ../README.md
:end-before: <!-- README only content
```

## Overview

The **bluesky-tiled-plugins** library provides tools for using [Tiled][] with
[Bluesky][]. It includes:

- A Bluesky callback for **saving metadata and data** from Bluesky documents in
  Tiled
- Classes that **extend the Tiled Python client** to display Bluesky-specific
  metadata and provide Bluesky-specific convenience methods
- Custom Tiled **search queries** targeting Bluesky-specific metadata like
  `ScanID` and `TimeRange`
- A Tiled exporter that **"replays" Bluesky documents** constructing them from
  saved data and metadata

Documentation is split into [four categories](https://diataxis.fr), also
accessible from links in the top bar.

<!-- https://sphinx-design.readthedocs.io/en/latest/grids.html -->

<!-- prettier-ignore-start -->

::::{grid} 2
:gutter: 4

:::{grid-item-card} {material-regular}`directions_walk;2em`
```{toctree}
:maxdepth: 2
tutorials
```
+++
Tutorials for installation and typical usage.
:::

:::{grid-item-card} {material-regular}`directions;2em`
```{toctree}
:maxdepth: 2
how-to
```
+++
Practical step-by-step guides and more advanced examples.
:::

:::{grid-item-card} {material-regular}`info;2em`
```{toctree}
:maxdepth: 2
explanations
```
+++
Explanations of how it works and why it works that way.
:::

:::{grid-item-card} {material-regular}`menu_book;2em`
```{toctree}
:maxdepth: 2
reference
```
+++
Technical reference material including APIs and release notes.
:::

::::

<!-- prettier-ignore-end -->

[Bluesky]: https://blueskyproject.io/bluesky
[Tiled]: https://blueskyproject.io/tiled
