# Search

A catalog of Bluesky Runs can be searched (filtered) based on metadata. The
metadata is drawn from the Bluesky documents that are issued at the beginning
and at the end of each Run: the _Run Start_ and _Run Stop_ documents.

A dot `.` can be used to traverse nested fields.

For this example we will use the public Tiled server at
`https://tiled-demo.nsls2.bnl.gov`.

```python
>>> from tiled.client import from_uri
>>> client = from_uri('https://tiled-demo.nsls2.bnl.gov')
>>> client
<Container {'bmm', 'csx', 'fxi'}>
```

We can look, for example, at some Bluesky data from the BMM beamline.

```python
>>> catalog = client['bmm']
>>> catalog
<Catalog {22521, 22524, 22525, 22526, 22528, 22542, 22545, ...} ~25 entries>
```

To get a sense of what we might search on, we might peek at the metadata on the
first item.

```python
>>> catalog.values().first().metadata
```

This is a nested dictionary. Here is an excerpt.

```python
{'start': {'XDI': {'Element': {'edge': 'K', 'symbol': 'Sc'}, ...}, ...}, ...}
```

We can see that the catalog in total has 25 scans. (We can see this in the
summary, or get it directly by using `len`.)

```
>>> catalog
<Catalog {22521, 22524, 22525, 22526, 22528, 22542, 22545, ...} ~25 entries>
>>> len(catalog)
25
```

If we narrow it down to "K-edge" scans only, we see 20 results.

```python
from tiled.queries import Key

catalog.search(Key('start.XDI.Element.edge') == 'K')
<Catalog {22521, 22524, 22525, 22526, 22528, 396, 397, 398, ...} ~20 entries>
```

Notice that the search method returns the same type object, just with filtered
contents. Thus, searches can be chained to progressively narrow results.

If we further narrow it to Scandium (`Sc`) we get down to four:

```python
>>> catalog.search(Key('start.XDI.Element.edge') == 'K').search(Key('start.XDI.Element.symbol') == 'Sc')
<Catalog {36495, 36502, 36508, 36509}>
```

We might stash that result in a variable and then peek at the first result.

```python
>>> results = catalog.search(Key('start.XDI.Element.edge') == 'K').search(Key('start.XDI.Element.symbol') == 'Sc')
>>> result = results.values().first()
>>> result
<BlueskyRun v3.0 streams: {'baseline', 'primary'} scan_id=36495 uid='903d6ca4' 2022-06-10 08:58>
```

From there, we can read the data into scientific Python data structures or
export it to a files.

```python
>>> result['baseline'].read()  # xarray.Dataset
>>> result['primary']['I0'][:]  # numpy array
>>> result['primary']['I0'].export('I0.csv')  # file
```

We can loop over the results to perform some batch operation over them:

```python
for result in results.values():
    # Do something
    ...
```

As a convenience, if the prefix `start.` or `stop.` is not specified, `start.`
will be searched by default.[^1]

```python
>>> catalog.search(Key("num_points") > 400)  # "num_points" -> "start.num_points"
<Catalog {22521, 22524, 22525, 22526, 22528, 396, 397, 398, ...} ~10 entries>
```

Tiled provides [built-in search queries][] covering most common use cases:
equality, comparison, full text, and more.

The `bluesky_tiled_plugins.queries` module adds some additional queries specific
to querying catalog of Bluesky Runs.

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
