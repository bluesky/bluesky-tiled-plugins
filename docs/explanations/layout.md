# Representation of Bluesky Runs in Tiled

[`Tiled`](https://blueskyproject.io/tiled/) is a data management system that
allows for the storage and retrieval of structured data along with the
associated metadata.

The `TiledWriter` callback is designed specifically for converting Bluesky run
documents into a format suitable for storage in a Tiled database.

It implicitly distinguishes between "internal" and "external" data. The internal
data are associated with the `Event` documents generated during a run; typically
this represents scalar measurements from sensors, motor positions, etc., which
are stored in a table with columns corresponding to different data keys and each
row representing a measurement at a single timestamp.

On the other hand, the external data are written by detectors directly to disk
and usually take the form of images or multidimensional arrays. The references
to the external files are provided in `StreamResource` (`Resource` in legacy
implementations) documents, which register the corresponding array-like
`DataSources` in Tiled. `StreamDatum` (or `Datum`) documents are processed via
the mechanism of `Consolidators` and determine the correspondence between the
indexing within these external arrays and the physically-meaningful sequence of
timestamps.

The time dimension (that is, the sequence of measurements) is usually shared
between internal and external data. Tiled handles this by writing all data from
the same Bluesky _stream_ into a container with a dedicated `"composite"` spec,
which tells the Tiled client how the data are aligned. Each stream node's
metadata includes the specifications for the related data keys as well as the
configuration parameters provided in the `EventDescriptor` document.

Finally, nodes for multiple streams are grouped together and placed into a
container for the entire run; its metadata contains the `Start` and `Stop`
documents. The Run container created by `TiledWriter` is designated with the
`BlueskyRun` version `3.0` spec to enable its back-compatibility with legacy
code via `bluesky-tiled-plugins`.

An example of the Tiled catalog structure for a Bluesky run might look like
this:

```
BlueskyRun <Container ("BlueskyRun_v3")>
│
├─ baseline <Container ("composite")>
│       ├─ internal <Table>   <-- data from Event documents
│       ├─ image_1 <Array>    <-- external (registered) data
│       │
│       │      ...
│       └─ image_n <Array>
│
├─ primary <Container ("composite")>
│       ├─ internal <Table>
│       ├─ image_1 <Array>
│       │
│       │      ...
│       └─ image_n <Array>
│
└─ third_stream <Container ("composite")>
```

> **Note**
>
> To be able to use `TiledWriter`, the Tiled server must be configured with an
> SQL catalog and an SQL-backed storage database for tabular data.
