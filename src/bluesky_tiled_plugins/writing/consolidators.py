import collections
import dataclasses
import warnings
from typing import Literal, cast, Optional

import numpy as np
from event_model.documents import EventDescriptor, StreamDatum, StreamResource
from tiled.mimetypes import DEFAULT_ADAPTERS_BY_MIMETYPE
from tiled.structures.array import ArrayStructure, BuiltinDtype, StructDtype
from tiled.structures.bytes import BytesStructure
from tiled.structures.core import StructureFamily
from tiled.structures.data_source import Asset, DataSource, Management
from ..utils import compile_template, list_summands, size_from_uri


@dataclasses.dataclass
class Patch:
    shape: tuple[int, ...]
    offset: tuple[int, ...]

    @classmethod
    def combine_patches(cls, patches: list["Patch"]) -> "Patch":
        """Combine multiple patches into a single patch

        The combined patch covers the union (smallest bounding box) of all provided patches.

        Parameters
        ----------
        patches : list[Patch]
            A list of Patch objects to combine.

        Returns
        -------
        Patch
            A new Patch object that covers the union of all input patches.
        """

        # Determine the overall shape and offset
        min_offset = list(patches[0].offset)
        max_extent = [
            offset + size for offset, size in zip(patches[0].offset, patches[0].shape)
        ]

        for patch in patches[1:]:
            for i in range(len(min_offset)):
                min_offset[i] = min(min_offset[i], patch.offset[i])
                max_extent[i] = max(max_extent[i], patch.offset[i] + patch.shape[i])

        combined_shape = tuple(
            max_e - min_o for min_o, max_e in zip(min_offset, max_extent)
        )
        combined_offset = tuple(min_offset)

        return cls(shape=combined_shape, offset=combined_offset)


class ConsolidatorBase:
    """Consolidator of StreamDatums

    A Consolidator consumes documents from RE; it is similar to usual Bluesky Handlers but is designed to work
    with streaming data (received via StreamResource and StreamDatum documents). It composes details (DataSource
    and its Assets) that will go into the Tiled database. Each Consolidator is instantiated per a Stream Resource.

    Tiled Adapters will later use this to read the data, with good random access and bulk access support.

    We put this code into consolidators so that additional, possibly very unusual, formats can be supported by
    users without getting a PR merged into Bluesky or Tiled.

    The CONSOLIDATOR_REGISTRY (see example below) and the Tiled catalog parameter adapters_by_mimetype can be used
    together to support:
        - Ingesting a new mimetype from Bluesky documents and generating DataSource and Asset with appropriate
          parameters (the consolidator's job);
        - Interpreting those DataSource and Asset parameters to do I/O (the adapter's job).

    To implement new Consolidators for other mimetypes, subclass ConsolidatorBase, possibly expand the
    `consume_stream_datum` and `get_data_source` methods, and ensure that the keys of returned `adapter_parameters`
    dictionary matches the expected adapter signature. Declare a set of supported mimetypes to allow validation and
    automated discovery of the subclassed Consolidator.

    Attributes
    ----------

    supported_mimetypes : set[str]
        a set of mimetypes that can be handled by a derived Consolidator class; raises ValueError if attempted to
        pass Resource documents related to unsupported mimetypes.

    join_method : Literal["stack", "concat"]
        a method to join the data; if "stack", the resulting consolidated dataset is produced by joining all datums
        along a new dimension added on the left, e.g. a stack of tiff images, otherwise -- datums will be appended
        to the end of the existing leftmost dimension, e.g. rows of a table (similarly to concatenation in numpy).
        The join_method can be overridden by the StreamResource parameter "join_method".
        The default join_method is currently "concat" but will change to "stack" in a future version.

    join_chunks : bool
        if True, the chunking of the resulting dataset will be determined after consolidation, otherwise each part
        is considered to be chunked separately.

    """

    supported_mimetypes: set[str] = {"application/octet-stream"}
    default_asset_role: str = "data_uris"  # Default parameter (role) for the asset(s)
    join_method: Literal["stack", "concat"] = "concat"
    join_chunks: bool = True

    def __init__(self, stream_resource: StreamResource, descriptor: EventDescriptor):
        self.mimetype = self.get_supported_mimetype(stream_resource)

        self.data_key = stream_resource["data_key"]
        self.uri = stream_resource["uri"]
        self.assets: list[Asset] = [
            Asset(
                data_uri=self.uri,
                is_directory=False,
                parameter=self.default_asset_role,
                num=0,
            )
        ]
        self._sres_parameters = stream_resource["parameters"]
        self._indx_offset = 0  # To reset file index counter for each new StreamResource

        # Warn when the join_method is not set explicitly and thus falls back to the default. The
        # default will change from "concat" to "stack" in a future version, which alters the shape of
        # the consolidated dataset, so downstream declared shapes may need to be updated accordingly.
        if "join_method" not in self._sres_parameters:
            warnings.warn(
                "The default value of join_method will change from 'concat' to 'stack' in a "
                "future version. This will change the shape of the consolidated dataset (a new "
                "leading dimension is added when stacking). Please set join_method explicitly in "
                "the StreamResource parameters and update the declared shape in the descriptor to "
                "account for this change.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Possibly overwrite the join_method and join_chunks attributes. This must happen before the datum
        # shape is determined below, since the shape computation depends on the join_method.
        self.join_method = self._sres_parameters.get("join_method", self.join_method)
        self.join_chunks = self._sres_parameters.get("join_chunks", self.join_chunks)

        # Any metadata to be set on the corresponding node in Tiled
        self.metadata: dict = {}
        if spec := self._sres_parameters.get("spec"):
            self.metadata["spec"] = spec

        # Find datum shape and machine dtype
        data_desc = descriptor["data_keys"][self.data_key]
        if None in data_desc["shape"]:
            raise NotImplementedError(
                f"Consolidator for {self.mimetype} does not support variable-sized data"
            )
        self.datum_shape: tuple[int, ...] = cast(
            "tuple[int, ...]", tuple(data_desc["shape"])
        )
        self.datum_shape = (
            ()
            if self.datum_shape == (1,) and self.join_method == "stack"
            else self.datum_shape
        )

        # Check that the datum shape is consistent between the StreamResource and the Descriptor
        if multiplier := self._sres_parameters.get("multiplier"):
            self.metadata["frame_per_point"] = multiplier
            self.datum_shape = self.datum_shape or (
                multiplier,
            )  # If datum_shape is not set
            if self.datum_shape[0] != multiplier:
                if self.datum_shape[0] == 1:
                    self.datum_shape = (multiplier,) + self.datum_shape[1:]
                else:
                    self.datum_shape = (multiplier,) + self.datum_shape
                    # TODO: Check consistency with chunk_shape

        # Determine the machine data type; fall back to np.dtype("float64") if not set
        self.data_type: BuiltinDtype | StructDtype
        dtype_descr = data_desc.get("dtype_numpy")
        if isinstance(dtype_descr, list):
            # np.dtype requires tuples in struct dtypes, not lists
            self.data_type = StructDtype.from_numpy_dtype(
                np.dtype(list(map(tuple, dtype_descr)))
            )
        else:
            self.data_type = BuiltinDtype.from_numpy_dtype(np.dtype(dtype_descr))

        # Set chunk (or partition) shape
        self.chunk_shape = self._sres_parameters.get("chunk_shape", ())
        if any(d <= 0 for d in self.chunk_shape):
            raise ValueError(
                f"Chunk size in all dimensions must be at least 1: chunk_shape={self.chunk_shape}."
            )

        # True chunking, if determined by the validator, is saved in data_source.properties
        self.orig_chunks: tuple[tuple[int, ...], ...] | None = None

        # Number of rows in the Data Source (all rows, includung skips)
        self._num_rows: int = 0
        self._seqnums_to_indices_map: dict[int, int] = {}

        # Set the dimension names if provided
        self.dims: tuple[str, ...] = tuple(data_desc.get("dims", ()))

    @classmethod
    def get_supported_mimetype(cls, sres):
        if (cls is not ConsolidatorBase) and (
            sres["mimetype"] not in cls.supported_mimetypes
        ):
            raise ValueError(
                f"A data source of {sres['mimetype']} type can not be handled by {cls.__name__}."
            )
        return sres["mimetype"]

    @property
    def shape(self) -> tuple[int, ...]:
        """Native shape of the data stored in assets

        This includes the leading (0th) dimension corresponding to the number of rows (if the join_method is stack)
        including skipped rows, if any. The number of relevant usable data rows may be lower, which is determined
        by the `seq_nums` field of StreamDatum documents."""

        if (self.join_method == "concat") and len(self.datum_shape) > 0:
            return self._num_rows * self.datum_shape[0], *self.datum_shape[1:]

        return self._num_rows, *self.datum_shape

    @property
    def chunks(self) -> tuple[tuple[int, ...], ...]:
        """Explicit (dask-style) specification of chunk sizes

        The produced chunk specification is a tuple of tuples of int that specify the sizes of each chunk in each
        dimension; it is based on the StreamResource parameter `chunk_shape`.

        If `chunk_shape` is an empty tuple -- assume the dataset is stored as a single chunk for all existing and
        new elements. Usually, however, `chunk_shape` is a tuple of int, in which case, we assume fixed-sized
        chunks with at most `chunk_shape[0]` elements (i.e. `_num_rows`); last chunk can be smaller. If chunk_shape
        is a tuple with less than `self.shape` elements -- assume it defines the chunk sizes along the leading
        dimensions.

        If the joining method is "concat", and `join_chunks = False`, the chunking along the leftmost dimensions
        is assumed to be preserved in each appended data point, i.e. consecutive chunks do not join, e.g. for a 1d
        array with chunks (3,3,1), the resulting chunking after 3 repeats is (3,3,1,3,3,1,3,3,1).
        When `join_chunks = True` (default), the chunk size along the leftmost dimension is determined by the
        chunk_shape parameter; this is the case when `join_method == "stack"` well.
        Chunking along the trailing dimensions is always preserved as in the original (single) array.
        """

        # If chunk shape is less than or equal to the total shape dimensions, chunk each specified dimension
        # starting from the leading dimension
        if len(self.chunk_shape) <= len(self.shape):
            if (
                self.join_method == "stack"
                or (self.join_method == "concat" and self.join_chunks)
                or len(self.chunk_shape) == 0
            ):
                result = tuple(
                    list_summands(ddim, cdim)
                    for ddim, cdim in zip(
                        self.shape[: len(self.chunk_shape)], self.chunk_shape
                    )
                )
            else:
                result = (
                    list_summands(
                        self.datum_shape[0], self.chunk_shape[0], repeat=self._num_rows
                    ),
                    *[
                        list_summands(ddim, cdim)
                        for ddim, cdim in zip(
                            self.shape[1 : len(self.chunk_shape)], self.chunk_shape[1:]
                        )
                    ],
                )
            return result + tuple((d,) for d in self.shape[len(self.chunk_shape) :])

        # If chunk shape is longer than the total shape dimensions, raise an error
        raise ValueError(
            f"The shape of chunks, {self.chunk_shape}, should be less than or equal to the shape of data, "
            f"{self.shape}."
        )

    @property
    def has_skips(self) -> bool:
        """Indicates whether any rows should be skipped when mapping their indices to frame numbers

        This flag is intended to provide a shortcut for more efficient data access when there are no skips, and the
        mapping between indices and seq_nums is straightforward. In other case, the _seqnums_to_indices_map needs
        to be taken into account.
        """
        return self._num_rows > len(self._seqnums_to_indices_map)

    def adapter_parameters(self) -> dict:
        """A dictionary of parameters passed to an Adapter

        These parameters are intended to provide any additional information required to read a data source of a
        specific mimetype, e.g. "path" the path into an HDF5 file or "template" the filename pattern of a TIFF
        sequence.

        This method is to be subclassed as necessary.
        """
        return {}

    def structure(self) -> ArrayStructure:
        return ArrayStructure(
            data_type=self.data_type,
            shape=self.shape,
            chunks=self.chunks,
            dims=self.dims if self.dims else None,
        )

    def consume_stream_datum(self, doc: StreamDatum):
        """Process a new StreamDatum and update the internal data structure

        This will be called for every new StreamDatum received to account for the new added rows.
        This method _may need_ to be subclassed and expanded depending on a specific mimetype.
        Actions:
          - Parse the fields in a new StreamDatum
          - Increment the number of rows (implemented by the Base class)
          - Keep track of the correspondence between indices and seq_nums (implemented by the Base class)
          - Update the list of assets, including their uris, if necessary
          - Update shape and chunks
        """
        old_shape = self.shape  # Adding new rows updates self.shape
        self._num_rows += doc["indices"]["stop"] - doc["indices"]["start"]
        new_seqnums = range(doc["seq_nums"]["start"], doc["seq_nums"]["stop"])
        new_indices = range(doc["indices"]["start"], doc["indices"]["stop"])
        self._seqnums_to_indices_map.update(dict(zip(new_seqnums, new_indices)))
        return Patch(
            offset=(old_shape[0], *[0 for _ in self.shape[1:]]),
            shape=(self.shape[0] - old_shape[0], *self.shape[1:]),
        )

    def get_data_source(self) -> DataSource:
        """Return a DataSource object reflecting the current state of the streamed dataset.

        The returned DataSource is conceptually similar (and can be an instance of) tiled.structures.DataSource. In
        general, it describes associated Assets (filepaths, mimetype) along with their internal data structure
        (array shape, chunks, additional parameters) and should contain all information necessary to read the file.
        """
        return DataSource(
            mimetype=self.mimetype,
            assets=self.assets,
            structure_family=StructureFamily.array,
            structure=self.structure(),
            parameters=self.adapter_parameters(),
            properties={"chunks": self.orig_chunks} if self.orig_chunks else {},
            management=Management.external,
        )

    def init_adapter(self, adapter_class=None):
        """Initialize a Tiled Adapter for reading the consolidated data

        Parameters
        ----------
        adapter_class : Optional[Type[Adapter]]
            An optional Adapter class to use for initialization; if not provided, the default adapter for the
            Consolidator's mimetype will be used.
        """

        adapter_class = adapter_class or DEFAULT_ADAPTERS_BY_MIMETYPE.get(self.mimetype)
        if not adapter_class:
            raise ValueError(f"No adapter found for mimetype {self.mimetype}")

        # Mimic the necessary aspects of a Tiled node with a namedtuple
        _Node = collections.namedtuple("Node", ["metadata_", "specs"])
        return adapter_class.from_catalog(
            self.get_data_source(), _Node({}, []), **self.adapter_parameters()
        )

    def update_from_stream_resource(self, stream_resource: StreamResource):
        """Consume an additional related StreamResource document for the same data_key"""

        raise NotImplementedError(
            "This method is not implemented in the base Consolidator class."
        )

    def validate(self, fix_errors=False) -> list[str]:
        """Validate the Consolidator's state against the expected structure"""

        # Initialize adapter from uris and determine the structure
        adapter_class = DEFAULT_ADAPTERS_BY_MIMETYPE[self.mimetype]
        uris = [asset.data_uri for asset in self.assets]
        structure = adapter_class.from_uris(
            *uris, **self.adapter_parameters()
        ).structure()
        notes = []

        # If this resource has the `frame_per_point`/`multiplier` parameter, the true shape of
        # the data is expected to be (num_events, multiplier, *rest) and needs to be adjusted
        if multiplier := self._sres_parameters.get("multiplier"):
            if structure.shape[0] % multiplier != 0:
                msg = (
                    "Expected the leftmost dimension of the data to be divisible by the "
                    f"`frame_per_point` multiplier of ({multiplier}), but got "
                    f"shape {structure.shape}. Ignoring the multiplier parameter."
                )
            else:
                orig_shape, self.orig_chunks = structure.shape, structure.chunks
                structure.shape = (
                    orig_shape[0] // multiplier,
                    multiplier,
                    *orig_shape[1:],
                )
                structure.chunks = (
                    list_summands(structure.shape[0], self.orig_chunks[0][0]),
                    (multiplier,),
                    *self.orig_chunks[1:],
                )
                msg = (
                    "Adjusted shape and chunks accorging to the `frame_per_point` "
                    f"multiplier of ({multiplier}): {orig_shape} -> {structure.shape}"
                )
            warnings.warn(msg, stacklevel=2)
            notes.append(msg)

        if self.shape != structure.shape:
            if not fix_errors:
                raise ValueError(f"Shape mismatch: {self.shape} != {structure.shape}")
            msg = f"Fixed shape mismatch: {self.shape} -> {structure.shape}"
            warnings.warn(msg, stacklevel=2)
            if self.join_method == "stack":
                self._num_rows = structure.shape[0]
                self.datum_shape = structure.shape[1:]
            elif self.join_method == "concat":
                # Estimate the number of frames_per_event (multiplier)
                multiplier = (
                    1
                    if structure.shape[0] % (structure.chunks[0][0] or 1)
                    else structure.chunks[0][0]
                ) or 1
                self._num_rows = structure.shape[0] // multiplier
                self.datum_shape = (multiplier,) + structure.shape[1:]
            notes.append(msg)

        if self.chunks != structure.chunks:
            if not fix_errors:
                raise ValueError(
                    f"Chunk shape mismatch: {self.chunks} != {structure.chunks}"
                )
            _chunk_shape = tuple(c[0] for c in structure.chunks)
            msg = f"Fixed chunk shape mismatch: {self.chunk_shape} -> {_chunk_shape}"
            warnings.warn(msg, stacklevel=2)
            self.chunk_shape = _chunk_shape
            notes.append(msg)

        if self.data_type != structure.data_type:
            if not fix_errors:
                raise ValueError(
                    f"dtype mismatch: {self.data_type} != {structure.data_type}"
                )
            msg = (
                f"Fixed dtype mismatch: {self.data_type.to_numpy_dtype()} "
                f"-> {structure.data_type.to_numpy_dtype()}"
            )
            warnings.warn(msg, stacklevel=2)
            self.data_type = structure.data_type
            notes.append(msg)

        if self.dims and (len(self.dims) != len(structure.shape)):
            if not fix_errors:
                raise ValueError(
                    f"Number of dimension names mismatch for a "
                    f"{len(structure.shape)}-dimensional array: {self.dims}"
                )
            old_dims = self.dims
            if len(old_dims) < len(structure.shape):
                self.dims = (
                    ("time",)
                    + old_dims
                    + tuple(
                        f"dim_{i}"
                        for i in range(len(old_dims) + 1, len(structure.shape))
                    )
                )
            else:
                self.dims = old_dims[: len(structure.shape)]
            msg = f"Fixed dimension names: {old_dims} -> {self.dims}"
            warnings.warn(msg, stacklevel=2)
            notes.append(msg)

        try:
            adapter = self.init_adapter()
        except Exception as e:
            raise RuntimeError(f"Adapter can not be initialized: {e}") from e
        if adapter is None:
            raise RuntimeError("Adapter can not be initialized")

        return notes

    def get_adapter(self, adapters_by_mimetype=None):
        warnings.warn(
            f"{self.__class__.__name__}.get_adapter is deprecated and will be removed in a future release; "
            f"please, use {self.__class__.__name__}.init_adapter instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        adapter_class = (adapters_by_mimetype or {}).get(self.mimetype)
        return self.init_adapter(adapter_class=adapter_class)


class BytesConsolidator:
    """Consolidator for opaque binary files registered as the `bytes` structure family.

    Unlike `ConsolidatorBase`, this consolidator does not interpret or reshape
    the data: each StreamDatum yields one or more `Asset`s carrying a file URI,
    and the corresponding Tiled node is a `bytes` node whose data can be
    downloaded but not sliced or read as an array.
    """

    supported_mimetypes: set[str] = {"application/octet-stream"}
    default_asset_role: str = "data_uris"  # Default parameter (role) for the asset(s)

    def __init__(self, stream_resource: StreamResource, descriptor: EventDescriptor):
        self.mimetype: str = self.get_supported_mimetype(stream_resource)
        self.metadata: dict = {}
        self.template: Optional[str] = None
        self.data_key: str = stream_resource["data_key"]
        self.uri: str = stream_resource["uri"]
        self.assets: list[Asset] = []
        self._indx_offset: int = 0

        self.update_from_stream_resource(stream_resource)

        # Preserve the originating Bluesky spec (if any) as metadata
        if spec := stream_resource["parameters"].get("spec"):
            self.metadata["spec"] = spec

    @classmethod
    def get_supported_mimetype(cls, sres):
        if sres["mimetype"] not in cls.supported_mimetypes:
            raise ValueError(
                f"A data source of {sres['mimetype']} type can not be handled by {cls.__name__}."
            )
        return sres["mimetype"]

    def get_datum_uri(self, indx: int):
        """Return a full uri for a datum (an individual file) based on its index in the sequence.

        This relies on the `template` parameter passed in the StreamResource, which is a string in the "new"
        Python formatting style that can be evaluated to a file name using the `.format(indx)` method given an
        integer index, e.g. "{:05d}.ext".

        The URI and the rendered template are concatenated verbatim, so
        the caller controls whether a `/` separator is present. Both
        conventions in the wild are supported: templates that render a
        bare filename appended to a filename-prefix URI (e.g. `.../uid`
        + `_{:d}.bin`) and templates that carry their own leading `/`
        appended to a directory URI (e.g. `.../dir` + `/{filename}.bin`).
        A single degenerate `//` at the junction (both sides carrying a
        slash) is collapsed to `/`.

        If template is not set, we assume that the uri is provided directly in the StreamResource document (i.e.
        a single file case), and return it as is.
        """

        if not self.template:
            return self.uri
        tail = self.template.format(indx - self._indx_offset)
        if self.uri.endswith("/") and tail.startswith("/"):
            tail = tail.lstrip("/")
        return self.uri + tail

    def consume_stream_datum(self, doc: StreamDatum):
        """Register one Asset per file index in the incoming StreamDatum."""
        first_file_indx = doc["indices"]["start"]
        last_file_indx = doc["indices"]["stop"]
        for indx in range(first_file_indx, last_file_indx):
            new_asset = Asset(
                data_uri=self.get_datum_uri(indx),
                is_directory=False,
                parameter=self.default_asset_role,
                num=len(self.assets),
            )
            self.assets.append(new_asset)

        return Patch(
            offset=(first_file_indx,), shape=(last_file_indx - first_file_indx,)
        )

    def update_from_stream_resource(self, stream_resource: StreamResource):
        "Update the consolidator with a new StreamResource document"

        self._sres_parameters = stream_resource["parameters"]
        if template := self._sres_parameters.get("template"):
            filename = self._sres_parameters.get("filename", "")
            self.template = compile_template(template, filename)

        # Increment the offset to reset the file index counter to start from "0" for the new StreamResource template
        self._indx_offset = len(self.assets)

    def validate(self, fix_errors: bool = False) -> list[str]:
        """Verify each registered asset is reachable; bytes payloads are otherwise opaque."""
        from .validator import AssetValidationException

        for ast in self.assets:
            try:
                size_from_uri(ast.data_uri)
            except (FileNotFoundError, OSError, ValueError) as e:
                raise AssetValidationException(
                    f"Could not determine size of asset {ast.data_uri}: {type(e).__name__}: {e}"
                ) from e
        return []

    def get_data_source(self) -> DataSource:
        return DataSource(
            mimetype=self.mimetype,
            assets=self.assets,
            structure_family=StructureFamily.bytes,
            structure=BytesStructure(),
            parameters={},
            properties={},
            management=Management.external,
        )


class CSVConsolidator(ConsolidatorBase):
    supported_mimetypes: set[str] = {"text/csv;header=absent"}
    join_method: Literal["stack", "concat"] = "concat"
    join_chunks: bool = False

    def adapter_parameters(self) -> dict:
        allowed_keys = {
            "assume_missing",
            "comment",
            "delimiter",
            "dtype",
            "encoding",
            "header",
            "names",
            "nrows",
            "sep",
            "skipfooter",
            "skiprows",
            "usecols",
        }
        return {
            k: v
            for k, v in {"header": None, **self._sres_parameters}.items()
            if k in allowed_keys
        }


class HDF5Consolidator(ConsolidatorBase):
    supported_mimetypes = {"application/x-hdf5"}

    def __new__(cls, stream_resource: StreamResource, descriptor: EventDescriptor):
        if stream_resource["parameters"].get("template"):
            return MultipartHDF5Consolidator(stream_resource, descriptor)
        return super().__new__(cls)

    def adapter_parameters(self) -> dict:
        """Parameters to be passed to the HDF5 adapter, a dictionary with the keys:

        dataset: str -- a path to the dataset within the hdf5 file
        swmr: bool -- True to enable the single writer / multiple readers regime
        locking: Optional[bool] -- True to enable file locking, False to disable,
            None to auto-detect (default)
        slice: Optional[tuple[str, ...]] -- a tuple of slice strings to select a subset of the dataset
        squeeze: bool -- whether to squeeze the dataset after slicing (default False)
        """
        params = {"dataset": self._sres_parameters["dataset"]}
        if slice := self._sres_parameters.get("slice", False):
            params["slice"] = slice
        if squeeze := self._sres_parameters.get("squeeze", False):
            params["squeeze"] = squeeze

        params["swmr"] = self._sres_parameters.get("swmr", True)
        params["locking"] = self._sres_parameters.get("locking", None)

        return params

    def update_from_stream_resource(self, stream_resource: StreamResource):
        """Add an Asset for a new StreamResource document"""
        if stream_resource["parameters"]["dataset"] != self._sres_parameters["dataset"]:
            raise ValueError(
                "All StreamResource documents must have the same dataset path."
            )
        if stream_resource["parameters"].get(
            "chunk_shape", ()
        ) != self._sres_parameters.get("chunk_shape", ()):
            raise ValueError(
                "All StreamResource documents must have the same chunk shape."
            )

        asset = Asset(
            data_uri=stream_resource["uri"],
            is_directory=False,
            parameter=self.default_asset_role,
            num=len(self.assets),
        )
        self.assets.append(asset)


class MultipartRelatedConsolidator(ConsolidatorBase):
    def __init__(
        self,
        stream_resource: StreamResource,
        descriptor: EventDescriptor,
    ):
        super().__init__(stream_resource, descriptor)
        self.assets.clear()  # Assets will be populated based on datum indices
        self.chunk_shape = self.chunk_shape or (1,)  # num frames per file

        # Ensure that the number of frames per file divides the number of frames per datum;
        # if not, silently coerce to a single file per datum but warn.
        if (self.join_method == "concat") and (
            self.datum_shape[0] % self.chunk_shape[0] != 0
        ):
            warnings.warn(
                f"Number of frames per file ({self.chunk_shape[0]}) does not divide "
                f"the total number of frames per datum ({self.datum_shape[0]}); "
                f"coercing chunk_shape to ({self.datum_shape[0]},) (one file per datum).",
                stacklevel=2,
            )
            self.chunk_shape = (self.datum_shape[0],)

        self._recompute_files_per_datum()

        # Compile and set the filename template
        self.template = compile_template(
            self._sres_parameters["template"], self._sres_parameters.get("filename", "")
        )

    def _recompute_files_per_datum(self):
        """Refresh cached files_per_datum from current sres_parameters and shape.

        Called at __init__ and again on each `update_from_stream_resource` so a
        subsequent StreamResource with a different `files_per_datum` (or implied
        by chunking) is honored.
        """
        self.files_per_datum = self._sres_parameters.get("files_per_datum") or (
            self.datum_shape[0] // self.chunk_shape[0]
            if self.join_method == "concat"
            else self.metadata.get("frame_per_point", 1)
        )

    def get_datum_uri(self, indx: int):
        """Return a full uri for a datum (an individual image file) based on its index in the sequence.

        This relies on the `template` parameter passed in the StreamResource, which is a string in the "new"
        Python formatting style that can be evaluated to a file name using the `.format(indx)` method given an
        integer index, e.g. "{:05d}.ext".

        The URI and the rendered template are concatenated verbatim, so the caller controls whether a `/` separator
        is present. Both conventions in the wild are supported: templates that render a bare filename appended
        to a filename-prefix URI (e.g. `.../uid` + `_{:d}.tif`) and templates that carry their own leading `/`
        appended to a directory URI (e.g. `.../dir` + `/{filename}.tif`).
        A single degenerate `//` at the junction (both sides carrying a slash) is collapsed to `/`.

        If template is not set, we assume that the uri is provided directly in the StreamResource document
        (i.e. a single file case), and return it as is.
        """

        if not self.template:
            return self.uri

        tail = self.template.format(indx - self._indx_offset)
        if self.uri.endswith("/") and tail.startswith("/"):
            tail = tail.lstrip("/")
        return self.uri + tail

    def consume_stream_datum(self, doc: StreamDatum):
        """Determine the number and names of files from indices of datums and the number of files per datum.

        In the most general case, each file may be a multipage tiff or a stack of images (frames) and a single
        datum may be composed of multiple such files, leading to a total of self.datum_shape[0] frames.
        Since each file necessarily represents a single chunk (tiffs can not be sub-chunked), the number of
        frames per file is equal to the leftmost chunk_shape dimension, self.chunk_shape[0].
        The number of files produced per each datum is then the ratio of these two numbers.

        If `join_method == "stack"`, we assume that each datum becomes its own index in the new leftmost dimension
        of the resulting dataset, and hence corresponds to a single file.
        """

        first_file_indx = doc["indices"]["start"] * self.files_per_datum
        last_file_indx = doc["indices"]["stop"] * self.files_per_datum
        for indx in range(first_file_indx, last_file_indx):
            new_asset = Asset(
                data_uri=self.get_datum_uri(indx),
                is_directory=False,
                parameter=self.default_asset_role,
                num=len(self.assets),
            )
            self.assets.append(new_asset)

        return super().consume_stream_datum(doc)

    def update_from_stream_resource(self, stream_resource: StreamResource):
        """Add an Asset for a new StreamResource document"""

        self._sres_parameters = stream_resource["parameters"]
        self.template = compile_template(
            self._sres_parameters["template"], self._sres_parameters.get("filename", "")
        )
        self._recompute_files_per_datum()
        # Increment the offset to reset the file index counter to start from "0" for the new StreamResource template
        self._indx_offset = len(self.assets)


class MultipartHDF5Consolidator(MultipartRelatedConsolidator):
    supported_mimetypes = {"application/x-hdf5"}

    def adapter_parameters(self) -> dict:
        return HDF5Consolidator.adapter_parameters(self)


class TIFFConsolidator(MultipartRelatedConsolidator):
    supported_mimetypes = {"multipart/related;type=image/tiff"}


class JPEGConsolidator(MultipartRelatedConsolidator):
    supported_mimetypes = {"multipart/related;type=image/jpeg"}


class NPYConsolidator(MultipartRelatedConsolidator):
    supported_mimetypes = {"multipart/related;type=application/x-npy"}
    join_method: Literal["stack", "concat"] = "stack"

    # NOTE: NPYConsolidator is tailored for tests in databroker with ophyd.sim devices.
    # Use with caution in other settings!

    def __init__(self, stream_resource: StreamResource, descriptor: EventDescriptor):
        # Unlike other image sequence formats (e.g. TIFF) the filename
        # template is hard-coded in the NPY_SEQ handler. We inject it
        # here so that the rest of the processing can be handled
        # generically by ConsolidatorBase.
        stream_resource["parameters"]["template"] = "%s_%d.npy"
        data_key = stream_resource["data_key"]
        datum_shape = descriptor["data_keys"][data_key]["shape"]
        stream_resource["parameters"]["chunk_shape"] = (1, *datum_shape)
        super().__init__(stream_resource, descriptor)


CONSOLIDATOR_REGISTRY = collections.defaultdict(
    lambda: ConsolidatorBase,
    {
        "application/octet-stream": BytesConsolidator,
        "text/csv;header=absent": CSVConsolidator,
        "application/x-hdf5": HDF5Consolidator,
        "multipart/related;type=image/tiff": TIFFConsolidator,
        "multipart/related;type=image/jpeg": JPEGConsolidator,
        "multipart/related;type=application/x-npy": NPYConsolidator,
    },
)


def consolidator_factory(stream_resource_doc, descriptor_doc):
    consolidator_class = CONSOLIDATOR_REGISTRY[stream_resource_doc["mimetype"]]
    return consolidator_class(stream_resource_doc, descriptor_doc)
