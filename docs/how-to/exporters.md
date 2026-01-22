# Replay Bluesky documents

The bluesky-tiled-plugings package provides a Tiled exporter that produces
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
