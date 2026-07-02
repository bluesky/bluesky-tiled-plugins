import copy
import json
import os
import re
from collections import defaultdict


def _synthesize_multipart_template(uris):
    """Infer an old-style filename template from a list of concrete URIs.

    Given URIs like ``file:///.../frame_000000.tif``, ``.../frame_000001.tif``,
    return ``(base_uri, template, start_index)`` such that
    ``base_uri + template % (start_index + i) == uris[i]`` for every ``i``.

    Returns ``(None, None, None)`` if the URIs don't fit a single numeric-field
    pattern; callers should fall back to a different strategy in that case.
    """
    if not uris:
        return None, None, None
    if len(uris) == 1:
        # Nothing to infer; treat the whole URI as the base.
        return uris[0], "", 0
    prefix = os.path.commonprefix(uris)
    slash = prefix.rfind("/")
    prefix = prefix[: slash + 1] if slash >= 0 else prefix
    suffix0 = uris[0][len(prefix) :]
    match = re.search(r"\d+", suffix0)
    if not match:
        return None, None, None
    lo, hi = match.span()
    width = hi - lo
    pre, post = suffix0[:lo], suffix0[hi:]
    start_index = int(suffix0[lo:hi])
    template = f"{pre}%0{width}d{post}"
    for i, u in enumerate(uris):
        if u != prefix + template % (start_index + i):
            return None, None, None
    return prefix, template, start_index


async def json_seq_exporter(mimetype, adapter, metadata, filter_for_access):
    """Export BlueskyRun as newline-delimited sequence of JSON documents.

    This callback is to be configured on the server-side to enable exporting
    BlueskyRun objects in JSON-Seq format.

    The resulting stream yields strings, each of which is a JSON document
    representing one of the standard Bluesky documents: start, descriptor,
    event, stream_resource, stream_datum, and stop, in the appropriate order.

    For example:

    ```
    {"name": "start", "doc": {...}}
    {"name": "descriptor", "doc": {...}}
    {"name": "event", "doc": {...}}
    {"name": "stream_resource", "doc": {...}}
    {"name": "stream_datum", "doc": {...}}
    ...
    {"name": "stop", "doc": {...}}
    ```
    """
    for spec in adapter.specs:
        if spec.name == "BlueskyRun" and spec.version.startswith("3."):
            break
    else:
        raise ValueError("This exporter only works with BlueskyRun v3.x")

    adapter = await filter_for_access(adapter)
    start_doc = {"name": "start", "doc": metadata.get("start", {})}
    result = []

    # Generate descriptors
    stream_names = await adapter.keys_range(offset=0, limit=None)
    if "streams" in stream_names:
        # Check for backward compatibility with the old layout (with an intermediate "streams" node)
        streams_adapter = await adapter.lookup_adapter(["streams"])
        if "BlueskyEventStream" not in {s.name for s in streams_adapter.specs}:
            adapter = streams_adapter
            stream_names = await adapter.keys_range(offset=0, limit=None)

    for desc_name in stream_names:
        desc_node = await adapter.lookup_adapter([desc_name])
        desc_meta = desc_node.metadata()
        part_names = set(
            await desc_node.keys_range(offset=0, limit=None)
        )  # Composite parts

        # First (or the only) descriptor
        desc_doc = {k: v for k, v in desc_meta.items() if k not in {"_config_updates"}}
        desc_doc["run_start"] = metadata.get("start", {}).get("uid")
        desc_doc["name"] = desc_name
        desc_doc["object_keys"] = defaultdict(list)
        for key, val in desc_doc["data_keys"].items():
            if obj_name := val.get("object_name"):
                desc_doc["object_keys"][obj_name].append(key)

        result.append({"name": "descriptor", "doc": desc_doc})

        # Process subsequent descriptors, if any
        desc_time_uids = [{"uid": desc_doc["uid"], "time": desc_doc["time"]}]
        for upd in desc_meta.get("_config_updates", []):
            desc_doc = copy.deepcopy(desc_doc)
            desc_doc["uid"] = upd["uid"]
            desc_doc["time"] = upd["time"]
            desc_time_uids.extend([{"uid": desc_doc["uid"], "time": desc_doc["time"]}])
            for obj_name, obj in upd.get("configuration", {}).items():
                # This assumes that that the full configuration was present in the first descriptor
                for key in obj["data"].keys():
                    desc_doc["configuration"][obj_name]["data"][key] = obj["data"][key]
                    desc_doc["configuration"][obj_name]["timestamps"][key] = obj[
                        "timestamps"
                    ][key]

            result.append({"name": "descriptor", "doc": desc_doc})

        # Generate events
        if "internal" in part_names:
            internal_node = await desc_node.lookup_adapter(["internal"])
            df = await internal_node.read()
            keys = [
                k
                for k in df.columns
                if k not in {"seq_num", "time"} and not k.startswith("ts_")
            ]
            for row in df.to_dict(orient="records"):
                desc_uid = desc_time_uids[0][
                    "uid"
                ]  # same as desc_node.metadata()["uid"] if no updates
                for _desc_uid_time in desc_time_uids[1:]:
                    if _desc_uid_time["time"] <= row["time"]:
                        desc_uid = _desc_uid_time["uid"]
                event_doc = {"seq_num": row["seq_num"], "time": row["time"]}
                event_doc["uid"] = (
                    f"event-{desc_uid}-{row['seq_num']}"  # can be anything (unique)
                )
                event_doc["descriptor"] = desc_uid
                event_doc["data"] = {
                    k: row[k].tolist() if hasattr(row[k], "__array__") else row[k]
                    for k in keys
                }
                event_doc["timestamps"] = {k: row[f"ts_{k}"] for k in keys}
                result.append({"name": "event", "doc": event_doc})

        # Generate Stream Resources and Datums
        desc_uid = desc_node.metadata()["uid"]
        for data_key in part_names.difference(("internal",)):
            # Loop over data_keys for external data only
            sres_uid = f"sr-{desc_uid}-{data_key}"  # can be anything (unique)
            ds = (await desc_node.lookup_adapter([data_key])).data_sources[0]
            asset_uris = [
                a.data_uri
                for a in sorted(ds.assets, key=lambda a: (a.num or 0))
                if a.parameter in {"data_uris", "data_uri"}
            ]
            parameters = dict(ds.parameters)

            total_shape = ds.structure.shape
            datum_shape = desc_node.metadata()["data_keys"][data_key]["shape"]
            # Infer join_method from the relationship between total_shape and
            # datum_shape. `stack` adds a leading dimension per datum;
            # `concat` merges datums along an existing leading dimension.
            is_stacked = len(total_shape) == len(datum_shape) + 1
            n_datums = (
                total_shape[0] if is_stacked else total_shape[0] // datum_shape[0]
            )

            # Multi-file (multipart) data sources persist adapter-only
            # parameters and drop the original filename template. Re-derive
            # a template from the concrete asset URIs so the emitted
            # stream_resource can be re-ingested by a MultipartRelated
            # consolidator.
            datum_offset = 0
            if len(asset_uris) > 1 and "template" not in parameters:
                base_uri, template, start_index = _synthesize_multipart_template(
                    asset_uris
                )
                if template is not None:
                    uri = base_uri
                    parameters["template"] = template
                    parameters.setdefault("chunk_shape", [1])
                    parameters.setdefault(
                        "join_method", "stack" if is_stacked else "concat"
                    )

                    # If files don't start at index 0, offset the datum
                    # indices so consolidator regenerates the same URIs.
                    if start_index and datum_shape[0]:
                        datum_offset = start_index // datum_shape[0]
                else:
                    uri = asset_uris[0]
            else:
                uri = asset_uris[0] if asset_uris else ds.assets[0].data_uri

            sres_doc = {
                "data_key": data_key,
                "uid": sres_uid,
                "run_start": metadata.get("start", {}).get("uid"),
                "mimetype": ds.mimetype,
                "parameters": parameters,
                "uri": uri,
            }
            result.append({"name": "stream_resource", "doc": sres_doc})

            # Generate a single stream_datum document for the entire stream
            sdat_uid = f"sd-{desc_uid}-{data_key}-0"  # can be anything (unique)
            sdat_doc = {
                "uid": sdat_uid,
                "stream_resource": sres_uid,
                "descriptor": desc_uid,
                "indices": {
                    "start": datum_offset,
                    "stop": datum_offset + n_datums,
                },
                "seq_nums": {
                    "start": datum_offset + 1,
                    "stop": datum_offset + n_datums + 1,
                },
            }
            result.append({"name": "stream_datum", "doc": sdat_doc})

    # Make sure that the order of documents is (approximately) correct
    result = sorted(
        result,
        key=lambda x: (
            x["doc"].get("time", float("inf")),
            {"stream_resource": 0, "stream_datum": 1}.get(x["name"], 2),
        ),
    )

    # Combine events into event_pages
    #     if modules_available("databroker"):
    #         from databroker.mongo_normalized import batch_documents
    #
    #         result = [
    #             {"name": x[0], "doc": x[1]}
    #             for x in batch_documents([(y["name"], y["doc"]) for y in result], size=1000)
    #         ]

    result.append({"name": "stop", "doc": metadata.get("stop", {})})

    # RFC 7464: each record is preceded by the ASCII record-separator
    # character (\x1E) and terminated by a newline.
    yield "\x1e" + json.dumps(start_doc) + "\n"
    for doc in result:
        yield "\x1e" + json.dumps(doc) + "\n"
