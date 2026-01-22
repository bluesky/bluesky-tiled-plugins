# Replay Bluesky documents

The `bluesky-tiled-plugins` package provides a Tiled exporter that produces
Bluesky documents, encoded as [newline-delimited JSON][].

This supports the `run.documents()` method in the Python client.

To use it, include the following in the Tiled server configuration.

```yaml
media_types:
  BlueskyRun:
    application/json-seq: bluesky_tiled_plugins.exporters:json_seq_exporter
```

Tiled does not store the documents in their original form. It stores a
consolidated representation of the metadata and data extracted from the
documents, which enables better read performance. Therefore, the exported
documents are reconstructed and they will not be an exact byte-by-byte
copy---e.g. the UIDs of individual `Datums` are not retained. However, they are
_semantically_ equivalent to the originals, and they "round trip" without loss
of any metadata or data. That is, if the exported documents are re-ingested with
`TiledWriter`, they are guaranteed to produce the same structure in Tiled.

[newline-delimited JSON]: https://github.com/ndjson/ndjson-spec

## Application Note: Missing media_types configuration (406 ClientError)

If the Tiled server is not configured with the `media_types` section shown
above, clients may encounter an error when requesting a Bluesky Run’s
document stream (JSON sequence).

### Symptom

A `ClientError` may be raised when calling a Run’s `documents()` generator.
This may happen either directly, or indirectly via higher-level helpers
such as `export()` (and potentially other APIs that consume `documents()`
under the hood).

Example error message:

```python
ClientError: 406: None of the media types requested by the client are supported. Supported: application/x-hdf5, application/json. Requested: application/json-seq.
```

### What triggers it

The following Python calls can trigger the error:

```python
# Direct use: request the document stream
for name, doc in run.documents():
    ...

# Indirect use: exporters commonly rely on documents()
run.export("something.ext")
```

### Why it happens

`run.documents()` requests the Run in JSON *sequence* format
(`application/json-seq`). If the server is not configured to advertise/support
that media type, Tiled responds with **HTTP 406 Not Acceptable**, indicating
that none of the client-requested media types are available.

### Resolution

Ensure the Tiled server configuration includes the `media_types` section
shown above (specifically enabling support for `application/json-seq`).
After adding it, restart the Tiled server so the updated media-type
configuration is applied.
