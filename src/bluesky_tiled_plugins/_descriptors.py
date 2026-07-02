"""Shared helper for reconstructing Event descriptor documents from the
stream metadata that both the client and the JSON-Seq exporter see.

The stored metadata on a Tiled node is user-arbitrary, so this helper
narrows the raw metadata down to the fields that make up a valid
`EventDescriptor` document (per the `event_model` schema) and validates
the result before returning it. Keeping the assembly logic in one
place ensures the client `descriptors` properties and the exporter
produce byte-identical documents.
"""

import copy
from collections import defaultdict

from event_model import DocumentNames, schema_validators

_DESCRIPTOR_VALIDATOR = schema_validators[DocumentNames.descriptor]

# Metadata keys that live on the Tiled stream node but are not part of
# the `EventDescriptor` schema and must be stripped before validation.
_NON_DESCRIPTOR_KEYS = frozenset({"_config_updates", "stream_name"})


def build_descriptor_docs(raw_metadata, stream_name, run_start_uid=None):
    """Reconstruct the sequence of `EventDescriptor` documents for a
    single stream from the raw metadata dictionary cached on its Tiled
    node.

    Parameters
    ----------
    raw_metadata : dict
        The `metadata()` payload of the stream node. May contain
        arbitrary keys; only the ones defined by the `EventDescriptor`
        schema are consumed. Any `_config_updates` list is unfolded
        into subsequent descriptor documents.
    stream_name : str
        The `name` field to place on every reconstructed descriptor.
    run_start_uid : str or None
        The UID of the `RunStart` document that owns this stream. When
        supplied, it is written to `run_start` on every descriptor.

    Returns
    -------
    list of dict
        One or more descriptor documents validated against the
        `event_model` `EventDescriptor` schema.
    """
    base = {k: v for k, v in raw_metadata.items() if k not in _NON_DESCRIPTOR_KEYS}
    base["name"] = stream_name
    if run_start_uid is not None:
        base["run_start"] = run_start_uid
    object_keys = defaultdict(list)
    for key, val in base.get("data_keys", {}).items():
        if obj_name := val.get("object_name"):
            object_keys[obj_name].append(key)
    base["object_keys"] = dict(object_keys)

    descriptors = [base]
    for upd in raw_metadata.get("_config_updates", []):
        desc = copy.deepcopy(descriptors[-1])
        if "uid" in upd:
            desc["uid"] = upd["uid"]
        if "time" in upd:
            desc["time"] = upd["time"]
        for obj_name, obj in upd.get("configuration", {}).items():
            for key in obj.get("data", {}):
                desc["configuration"][obj_name]["data"][key] = obj["data"][key]
                desc["configuration"][obj_name]["timestamps"][key] = obj["timestamps"][
                    key
                ]
        descriptors.append(desc)

    for desc in descriptors:
        _DESCRIPTOR_VALIDATOR.validate(desc)
    return descriptors
