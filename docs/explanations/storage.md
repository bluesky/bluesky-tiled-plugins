# Where is Bluesky data stored

The metadata extracted from Run Start, Event Descriptor, and Run Stop documents
is stored in the `tiled_catalog` PostgreSQL database.

Pointers to large array data (e.g., images) extracted from (Stream) Resource and
(Stream) Datum documents are also stored in the `tiled_catalog` PostgreSQL
database.

Data from Event documents is generally stored in tables in the `tiled_storage`
database, but there are some exceptions.

Bluesky permits array data to be placed directly into Event documents. This is
intended for small arrays, such as bounding boxes and configuration settings,
but there is no hard limit on what Bluesky will accept. When TiledWriter
receives Event documents that are any of the following:

- large
- N-dimensional (i.e., more than 1-dimensional)
- ragged (non-rectangular)

it writes the data as an array. Tiled will store this in Zarr format, either in
a filesystem or S3 storage, depending on which is configured in
`writable_storage` with higher precedence. The cutoff for "large" is controlled
by the global variable
`bluesky_tiled_plugins.writing.tiled_writer.MAX_ARRAY_SIZE`. It can be adjusted,
but if it is set _too_ high PostgreSQL's own limits can be reached.

If you avoid placing large, N-dimeinsional, or ragged data directly into Event
documents, is it possible to write Bluesky data into Tiled without any
filesystem or S3 storage configured.
