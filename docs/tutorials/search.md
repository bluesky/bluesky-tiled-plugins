# Search

A catalog of Bluesky Runs can be searched, on metadata drawn from the Bluesky
documents that are issues at the beginning and end of each Run (the Run Start
and Run Stop documents). A dot `.` can be used to traverse nested fields.

```python
from tiled.queries import Key

catalog.search(Key("start.num_points") > 3)
catalog.search(Key("start.sample.element") == "Ni")
catalog.search(Key("stop.exit_status") == "success")
```

As a convenience, if the prefix `start.` or `stop.` is not specified, `start.`
will be searched by default.[^1]

```python
catalog.search(Key("num_points") > 3)  # "num_points" -> "start.num_points"
```

Queries can be chained to progressively narrow results:

```python
catalog.search(...).search(...).search(...)
```

Tiled provides [built-in search queries][] covering most common use cases:
equality, comparison, full text, and more.

The `bluesky_tiled_plugins.queries` modules adds some additional queries
specific to querying catalog of Bluesky Runs.

- {py:func}`bluesky_tiled_plugins.queries.PartialUID`
- {py:func}`bluesky_tiled_plugins.queries.ScanID`
- {py:class}`bluesky_tiled_plugins.queries.ScanIDRange`
- {py:class}`bluesky_tiled_plugins.queries.TimeRange`

For backward-compatibility with common legacy workflows, item lookup on a
Catalog of Bluesky Runs integrates these queries:

- `catalog[<positive integer>]` searches by scan ID.
- `catalog[<partial uid>]` searches by partial UID.

[built-in search queries]:
  https://blueskyproject.io/tiled/reference/queries.html

[^1]: This is a convenience provided by a [custom client](#custom-clients).
