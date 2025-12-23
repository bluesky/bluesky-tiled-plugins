(custom-clients)=

# Custom Tiled Clients

## All You Need To Know

This package provides a set of custom Python clients designed to facilitate
interaction with Bluesky data stored in Tiled catalogs.

Simply _installing_ `bluesky-tiled-plugins` registers these clients with Tiled
so it will automatically discover and use them.

Navigating a Tiled catalog **with** `bluesky-tiled-plugins` installed, we
readily see scientifically-useful information, such as the `scan_id`s of Bluesky
runs:

```
 <Catalog {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ...} ~241632 entries>
 <BlueskyRun v2.0 {'baseline', 'primary'} scan_id=42860 uid='1ae45615' 2025-11-23 13:48>
```

Navigating **without** `bluesky-tiled-plugins` installed, we see more generic
information displayed, e.g. `uuid`s of Bluesky runs:

```
<Container {'c7cfa95c-daef-4bb1-bb8f-8d1a4f25e5dc', ...} ~241637 entries>
<Container {'baseline', 'primary'}>
```

There are also convenience properties, for example:

```
run.start  # alias for run.metadata["start"]
run.stop  # alias for run.metatadata["stop"]
```

All the data and metadata is still _accessible_ without `bluesky-tiled-plugins`;
it's just less conveniently presented.

## Debugging

Tiled uses [Python entrypoints][] to discover these plugins. To test that they
are working:

```python
import entrypoints

entrypoints.get_group_named("tiled.special_client")
```

The output should include:

```python
{
    "BlueskyEventStream": EntryPoint(
        "BlueskyEventStream",
        "bluesky_tiled_plugins.clients.bluesky_event_stream",
        "BlueskyEventStream",
        Distribution("bluesky_tiled_plugins", "2.0.0rc1"),
    ),
    "BlueskyRun": EntryPoint(
        "BlueskyRun",
        "bluesky_tiled_plugins.clients.bluesky_run",
        "BlueskyRun",
        Distribution("bluesky_tiled_plugins", "2.0.0rc1"),
    ),
    "CatalogOfBlueskyRuns": EntryPoint(
        "CatalogOfBlueskyRuns",
        "bluesky_tiled_plugins.clients.catalog_of_bluesky_runs",
        "CatalogOfBlueskyRuns",
        Distribution("bluesky_tiled_plugins", "2.0.0rc1"),
    ),
}
```

If it is does not, reinstall `bluesky-tiled-plugins`.

[Python entrypoints]:
  https://packaging.python.org/en/latest/specifications/entry-points/
