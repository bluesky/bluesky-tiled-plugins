import codecs
import copy
import functools
import httpx
import io
import json
import keyword
import warnings
from datetime import datetime

from tiled.client.container import Container
from tiled.client.utils import handle_error, retry_context
from tiled.utils import safe_json_dump
from tiled.type_aliases import JSON_ITEM

from ._common import IPYTHON_METHODS
from .bluesky_event_stream import BlueskyEventStreamV2SQL
from .document import (
    DatumPage,
    Descriptor,
    Event,
    EventPage,
    Resource,
    Start,
    Stop,
    StreamDatum,
    StreamResource,
)
from ..writing.validator import validate, ValidationException

_document_types = {
    "start": Start,
    "stop": Stop,
    "event": Event,
    "descriptor": Descriptor,
    "event_page": EventPage,
    "datum_page": DatumPage,
    "resource": Resource,
    "stream_resource": StreamResource,
    "stream_datum": StreamDatum,
}


class BlueskyRun(Container):
    _ipython_display_ = None
    _repr_mimebundle_ = None

    def __new__(cls, context, *, item, structure_clients, **kwargs):
        # When inheriting from BlueskyRun, return the class itself
        if cls is not BlueskyRun:
            return super().__new__(cls)

        # Set the version based on the specs
        _cls = BlueskyRunV3 if cls._is_sql(item) else BlueskyRunV2Mongo
        return _cls(context, item=item, structure_clients=structure_clients, **kwargs)

    @staticmethod
    def _is_sql(item) -> bool:
        for spec in item["attributes"]["specs"]:
            if spec["name"] == "BlueskyRun":
                if spec["version"].startswith("3."):
                    return True
                return False

    def __repr__(self) -> str:
        metadata = self.metadata
        datetime_ = datetime.fromtimestamp(metadata["start"]["time"])
        return (
            f"<BlueskyRun v{self._version} "
            f"{set(self)!r} "  # show the keys
            f"scan_id={metadata['start'].get('scan_id', 'UNSET')!s} "  # (scan_id is optional in the schema)
            f"uid={metadata['start']['uid'][:8]!r} "  # truncated uid
            f"{datetime_.isoformat(sep=' ', timespec='minutes')}"
            ">"
        )

    @property
    def start(self) -> dict[str, JSON_ITEM]:
        """
        The Run Start document. A convenience alias:

        >>> run.start is run.metadata["start"]
        True
        """
        return self.metadata["start"]

    @property
    def stop(self) -> dict[str, JSON_ITEM]:
        """
        The Run Stop document. A convenience alias:

        >>> run.stop is run.metadata["stop"]
        True
        """
        return self.metadata["stop"]

    @functools.cached_property
    def descriptors(self) -> list[dict[str, JSON_ITEM]]:
        return [doc for name, doc in self.documents() if name == "descriptor"]

    def __getattr__(self, key):
        """
        Let run.X be a synonym for run['X'] unless run.X already exists.

        This behavior is the same as with pandas.DataFrame.
        """
        # The wisdom of this kind of "magic" is arguable, but we
        # need to support it for backward-compatibility reasons.
        if key in IPYTHON_METHODS:
            raise AttributeError(key)
        if key in self:
            return self[key]
        raise AttributeError(key)

    def __dir__(self):
        # Build a list of entries that are valid attribute names
        # and add them to __dir__ so that they tab-complete.
        tab_completable_entries = [
            entry
            for entry in self
            if (entry.isidentifier() and (not keyword.iskeyword(entry)))
        ]
        return super().__dir__() + tab_completable_entries

    def describe(self) -> dict[str, dict[str, JSON_ITEM]]:
        "For back-compat with intake-based BlueskyRun"
        warnings.warn(
            "This will be removed. Use .metadata directly instead of describe()['metadata'].",
            DeprecationWarning,
            stacklevel=2,
        )
        return {"metadata": self.metadata}

    def __call__(self):
        warnings.warn(
            "Do not call a BlueskyRun. For now this returns self, for "
            "backward-compatibility. but it will be removed in a future "
            "release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self

    def read(self):
        raise NotImplementedError(
            "Reading any entire run is not supported. Access a stream in this run and read that."
        )

    @property
    def base(self) -> Container:
        "Return the base Container client instead of a BlueskyRun client"
        return Container(
            self.context,
            item=self.item,
            structure_clients=self.structure_clients,
            queries=self._queries,
            sorting=self._sorting,
            include_data_sources=self._include_data_sources,
        )

    to_dask = read


class BlueskyRunV2(BlueskyRun):
    """A MongoDB-native layout of BlueskyRuns

    This layout has been in use prior to the introduction of SQL backend in May 2025.
    """

    _version = "2.0"

    def __new__(cls, context, *, item, structure_clients, **kwargs):
        # When inheriting, return the class itself
        if cls is not BlueskyRunV2:
            return super().__new__(
                cls, context, item=item, structure_clients=structure_clients, **kwargs
            )

        _cls = BlueskyRunV2SQL if cls._is_sql(item) else BlueskyRunV2Mongo
        return _cls(context, item=item, structure_clients=structure_clients, **kwargs)

    @property
    def v1(self):
        "Accessor to legacy interface."
        from databroker.v1 import Broker, Header

        db = Broker(self)
        header = Header(self, db)
        return header

    @property
    def v2(self) -> "BlueskyRunV2":
        return self

    @property
    def v3(self) -> "BlueskyRunV3":
        if not self._is_sql(self.item):
            raise NotImplementedError(
                "v3 is not available for MongoDB-based BlueskyRun"
            )

        structure_clients = copy.copy(self.structure_clients)
        structure_clients.set("BlueskyRun", lambda: BlueskyRunV3)
        return BlueskyRunV3(
            self.context, item=self.item, structure_clients=structure_clients
        )


class BlueskyRunV2Mongo(BlueskyRunV2):
    def documents(self, fill=False):
        if fill == "yes":
            fill = True
        elif fill == "no":
            fill = False
        elif fill == "delayed":
            raise NotImplementedError("fill='delayed' is not supported")
        else:
            fill = bool(fill)
        link = self.item["links"]["self"].replace("/metadata", "/documents", 1)
        for attempt in retry_context():
            with attempt:
                with self.context.http_client.stream(
                    "GET",
                    link,
                    params={"fill": fill},
                    headers={"Accept": "application/json-seq"},
                ) as response:
                    if response.is_error:
                        response.read()
                        handle_error(response)
                    tail = ""
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    for chunk in response.iter_bytes():
                        for line in decoder.decode(chunk).splitlines(keepends=True):
                            if line[-1] == "\n":
                                item = json.loads(tail + line)
                                yield (
                                    item["name"],
                                    _document_types[item["name"]](item["doc"]),
                                )
                                tail = ""
                            else:
                                tail += line
                    tail += decoder.decode(b"", final=True)
                    if tail:
                        item = json.loads(tail)
                        yield (item["name"], _document_types[item["name"]](item["doc"]))


class _BlueskyRunSQL(BlueskyRun):
    """A base class for a BlueskyRun that is backed by a SQL database.

    This class implements the SQL-specific method for accessing the stream of
    Bluesky documents. It is not intended to be used directly, but rather as a
    base class for other classes (v2 and v3) that implement additional methods.
    """

    @functools.cached_property
    def _has_streams_namespace(self) -> bool:
        """Determine whether the BlueskyRun has an intermediate "streams" namespace.

        Maintained for backward compatibility. Returns True if the following conditions are met:
        1. There is a "streams" key in the base container.
        2. The specs of the "streams" container do not include "BlueskyEventStream",
           indicating that "streams" is not itself a BlueskyEventStream.
        """
        return ("streams" in self.base.keys()) and (
            "BlueskyEventStream" not in {s.name for s in self.base["streams"].specs}
        )

    @functools.cached_property
    def _stream_names(self) -> list[str]:
        """Get the sorted list of stream names in the BlueskyRun.

        This property accounts for both the new layout (without "streams" namespace)
        and the old layout (with "streams" namespace), in which case the stream names
        are derived from the keys under the "streams" namespace.
        """

        return sorted(
            k
            for k in (
                self.base["streams"] if self._has_streams_namespace else self.base
            )
        )

    def __getitem__(self, key):
        if isinstance(key, tuple):
            key = "/".join(key)

        base_class = super()  # The base Container class

        def _base_getitem(key):
            # Try to get the item directly from the new container layout. Consider nested keys.
            try:
                return base_class.__getitem__(key)
            except KeyError as e:
                try:
                    # The requested key might be a column in the "internal" table
                    key = key.split("/")
                    key.insert(-1, "internal")
                    return base_class.__getitem__("/".join(key))
                except KeyError:
                    raise KeyError(
                        f"Key '{key[-1]}' not found in the BlueskyRun container"
                    ) from e

        # Back-compatibility for old versions of BlueskyRun layout that included 'streams' namespace.
        # This takes into account the possibility of an actual BlueskyEventStream to be named 'streams'.
        try:
            return _base_getitem(key)
        except KeyError as e:
            if key == "streams":
                warnings.warn(
                    "Looks like you are trying to access the 'streams' namespace, "
                    "but there is no 'streams' namespace in this BlueskyRun, which follows the new layout. "
                    "Please use the stream names directly, e.g. run['primary'] instead of run['streams/primary'].",
                    DeprecationWarning,
                    stacklevel=2,
                )
                return self
            if key.split("/")[0] != "streams":
                try:
                    result = _base_getitem("streams/" + key)
                    warnings.warn(
                        f"Key '{key}' not found directly in the BlueskyRun container. "
                        "Trying to access it via the 'streams' namespace for backward-compatibility. "
                        "This behavior is deprecated and will be removed in a future release. "
                        "Please consider migrating the catalog structure to the new layout.",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                    return result
                except KeyError:
                    raise KeyError from e
            elif key.split("/")[0] == "streams":
                try:
                    result = _base_getitem(key[len("streams/") :])
                    warnings.warn(
                        f"Looks like you are trying to access '{key}' via a 'streams' namespace, "
                        "but there is no 'streams' namespace in this BlueskyRun, which follows the new layout. "
                        f"Please access the stream directly, e.g. run['{key}'] instead of run['streams/{key}'].",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                    return result
                except KeyError:
                    raise KeyError from e
            else:
                raise KeyError from e

    def _keys_slice(
        self, start, stop, direction, page_size: int | None = None, **kwargs
    ):
        sorted_keys = (
            self._stream_names[::-1] if direction < 0 else self._stream_names
        )
        return (yield from sorted_keys[start:stop])

    def _items_slice(
        self, start, stop, direction, page_size: int | None = None, **kwargs
    ):
        sorted_keys = (
            self._stream_names[::-1] if direction < 0 else self._stream_names
        )
        for key in sorted_keys[start:stop]:
            yield key, self[key]

    def __iter__(self):
        yield from self._stream_names

    def documents(self, fill=False):
        if fill:
            raise NotImplementedError(
                "documents(fill=True) is not supported for SQL-based BlueskyRun clients"
            )

        with io.BytesIO() as buffer:
            self.export(buffer, format="application/json-seq")
            buffer.seek(0)
            for line in buffer:
                stripped = line.decode().strip()
                if not stripped:
                    continue
                parsed = json.loads(stripped)
                yield parsed["name"], _document_types[parsed["name"]](parsed["doc"])


class BlueskyRunV2SQL(BlueskyRunV2, _BlueskyRunSQL):
    def __getitem__(self, key):
        # For v2, we need to handle the streams and configs keys specially
        if isinstance(key, tuple):
            key = "/".join(key)

        key, *rest = key.split("/", 1)

        if key == "streams":
            raise KeyError(
                "Looks like you are trying to access the 'streams' namespace, "
                "but this pathway has never been supported in the .v2 BlueskyRun client. "
                "Please access the stream directly, e.g. run['primary']."
            )

        stream_composite_client = super().__getitem__(key)
        stream_container = BlueskyEventStreamV2SQL.from_stream_client(
            stream_composite_client
        )

        return stream_container[rest[0]] if rest else stream_container


class BlueskyRunV3(_BlueskyRunSQL):
    """A BlueskyRun that is backed by a SQL database."""

    _version = "3.0"

    def __new__(cls, context, *, item, structure_clients, **kwargs):
        # When inheriting, return the class itself
        if cls is not BlueskyRunV3 or cls._is_sql(item):
            return super().__new__(
                cls, context, item=item, structure_clients=structure_clients, **kwargs
            )
        return BlueskyRunV2Mongo(
            context, item=item, structure_clients=structure_clients, **kwargs
        )

    def __getattr__(self, key):
        # A shortcut to the stream data
        if key in self._stream_names:
            return self["streams"][key] if self._has_streams_namespace else self[key]

        return super().__getattr__(key)

    def __repr__(self):
        metadata = self.metadata
        datetime_ = datetime.fromtimestamp(metadata["start"]["time"])
        return (
            f"<BlueskyRun v{self._version} "
            f"streams: {set(self._stream_names) or 'NONE'} "
            f"scan_id={metadata['start'].get('scan_id', 'UNSET')!s} "  # (scan_id is optional in the schema)
            f"uid={metadata['start']['uid'][:8]!r} "  # truncated uid
            f"{datetime_.isoformat(sep=' ', timespec='minutes')}"
            ">"
        )

    @property
    def v1(self):
        "Access to legacy interface"
        return self.v2.v1

    @property
    def v2(self) -> BlueskyRunV2:
        structure_clients = copy.copy(self.structure_clients)
        structure_clients.set("BlueskyRun", lambda: BlueskyRunV2)
        return BlueskyRunV2(
            self.context, item=self.item, structure_clients=structure_clients
        )

    @property
    def v3(self) -> "BlueskyRunV3":
        return self

    def validate(
        self,
        fix_errors=True,
        try_reading=True,
        raise_on_error=False,
        ignore_errors=None,
        write_notes=True,
    ):
        """Validate for for completeness and data integrity.

        Parameters
        ----------
        fix_errors : bool, optional
            Whether to attempt to fix structural errors found during validation.
            Default is True.
        try_reading : bool, optional
            Whether to attempt reading the data for external data keys.
            Default is True.
        raise_on_error : bool, optional
            Whether to raise an exception on the first validation error encountered.
            Default is False.
        ignore_errors : list of str, optional
            List of error messages to ignore during validation. If any errors whose
            message matches one of the patterns in this list are encountered, they will
            be logged, but the validation of the remaining data keys will continue.
        write_notes : bool, optional
            Whether to write validation notes to the root client's metadata.
            Default is True.

        Returns
        -------
        bool
            True if the data structure is valid and reading succeeded, False otherwise.
        """

        is_valid = False

        for attempt in retry_context():
            with attempt:
                response = self.context.http_client.post(
                    self.uri.replace("/api/v1/metadata/", "/custom/validate/", 1),
                    params={"fix": fix_errors, "read": try_reading},
                    content=safe_json_dump({"ignore_errors": ignore_errors}),
                )

        try:
            content = handle_error(response).json()
            is_valid, notes = content.get("valid"), content.get("notes", [])

            if is_valid:
                for note in notes:
                    warnings.warn(note, stacklevel=2)

                if notes and write_notes:
                    existing_notes = self.metadata.get("notes", [])
                    self.update_metadata(
                        {"notes": existing_notes + notes}, drop_revision=True
                    )
            elif raise_on_error:
                msg = "Remote validation failed: " + "; ".join(notes)
                raise ValidationException(msg, self.item["id"])

        except httpx.HTTPStatusError as e:
            # Backcompatibility: if the server does not support validation endpoint,
            # it will return 404 Not Found error; in this case, attempt to validate
            # the data structure locally by the client itself (requires multiple
            # round-trips to the server, but better than nothing).

            if response.status_code == httpx.codes.NOT_FOUND:
                warnings.warn(
                    "Tiled server does not support remote validation. "
                    "Attempting to validate the data structure by the client.",
                    stacklevel=2,
                )
                return validate(
                    self,
                    fix_errors=fix_errors,
                    try_reading=try_reading,
                    raise_on_error=raise_on_error,
                    ignore_errors=ignore_errors,
                    write_notes=write_notes,
                )
            elif raise_on_error:
                msg = (
                    "Remote validation request failed with status code "
                    f"{response.status_code}: {response.text}"
                )
                raise ValidationException(msg, self.item["id"]) from e

        return is_valid
