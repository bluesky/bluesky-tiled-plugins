import logging
import time

from tiled.client.array import ArrayClient
from tiled.client.dataframe import DataFrameClient
from tiled.client.utils import handle_error
from tiled.mimetypes import DEFAULT_ADAPTERS_BY_MIMETYPE as ADAPTERS_BY_MIMETYPE
from tiled.utils import safe_json_dump
import re

logger = logging.getLogger(__name__)


class ValidationException(Exception):
    def __init__(self, message, uid=None):
        super().__init__(message)
        self.uid = uid


class ReadingValidationException(ValidationException):
    pass


class RunValidationException(ValidationException):
    pass


class MetadataValidationException(ValidationException):
    pass


def validate(
    root_client,
    fix_errors=True,
    try_reading=True,
    raise_on_error=False,
    ignore_errors=[],
):
    """Validate the given BlueskyRun client for completeness and data integrity.

    Parameters
    ----------

    root_client : tiled.client.run.RunClient
        The Run client to validate.
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
        List of error messages to ignore during reading validation.
        Default is an empty list.

    Returns
    -------
    bool
        True if validation passed without errors, False otherwise.
    """

    # Check if there's a Stop document in the run
    if "stop" not in root_client.metadata:
        logger.error("The Run is not complete: missing the Stop document")
        if raise_on_error:
            raise RunValidationException("Missing Stop document in the run")

    # Check all streams and data keys
    errored_keys, notes = [], []
    streams_node = (
        root_client["streams"] if "streams" in root_client.keys() else root_client
    )
    for sname, stream in streams_node.items():
        for data_key in stream.base:
            if data_key == "internal":
                continue

            data_client = stream[data_key]
            if data_client.data_sources()[0].management != "external":
                continue

            # Validate data structure
            title = f"Validation of data key '{sname}/{data_key}'"
            try:
                _notes = validate_structure(data_client, fix_errors=fix_errors)
                notes.extend([title + ": " + note for note in _notes])
            except Exception as e:
                msg = (
                    f"{type(e).__name__}: "
                    + str(e).replace("\n", " ").replace("\r", "").strip()
                )
                msg = title + f" failed with error: {msg}"
                logger.error(msg)
                if raise_on_error:
                    raise e
                notes.append(msg)

            # Validate reading of the data
            if try_reading:
                try:
                    validate_reading(data_client, ignore_errors=ignore_errors)
                except Exception as e:
                    errored_keys.append((sname, data_key, str(e)))
                    logger.error(
                        f"Reading validation of '{sname}/{data_key}' failed with error: {e}"
                    )
                    if raise_on_error:
                        raise e

            time.sleep(0.1)

    if try_reading and (not errored_keys):
        logger.info("Reading validation completed successfully.")

    # Update the root metadata with validation notes
    if notes:
        existing_notes = root_client.metadata.get("notes", [])
        root_client.update_metadata(
            {"notes": existing_notes + notes}, drop_revision=True
        )

    return not errored_keys


def validate_reading(data_client, ignore_errors=[]):
    """Attempt to read data from the given data client to validate data accessibility

    Parameters
    ----------
        data_client : tiled.client.ArrayClient or tiled.client.DataFrameClient
            The data client to validate reading from.
        ignore_errors : list of str, optional
            List of error messages to ignore during reading validation.
            Default is an empty list.

    Raises
    ------
        ReadingValidationException
            If reading the data fails with an unignored error.
    """

    data_key = data_client.item["id"]
    sname = data_client.item["attributes"]["ancestors"][-1]  # stream name

    if isinstance(data_client, ArrayClient):
        try:
            # Try to read the first and last elements
            idx_left_top = (0,) * len(data_client.shape)
            data_client[idx_left_top]
            idx_right_bottom = (-1,) * len(data_client.shape)
            data_client[idx_right_bottom]
        except Exception as e:
            if any([re.search(msg, str(e)) for msg in ignore_errors]):
                logger.info(f"Ignoring array reading error: {sname}/{data_key}: {e}")
            else:
                raise ReadingValidationException(
                    f"Array reading failed with error: {e}"
                )

    elif isinstance(data_client, DataFrameClient):
        try:
            data_client.read()  # try to read the entire table
        except Exception as e:
            if any([re.search(msg, str(e)) for msg in ignore_errors]):
                logger.info(f"Ignoring table reading error: {sname}/{data_key}: {e}")
            else:
                raise ReadingValidationException(
                    f"Table reading failed with error: {e}"
                )

    else:
        logger.warning(
            f"Validation of '{data_key=}' is not supported with client of type {type(data_client)}."
        )


def validate_structure(data_client, fix_errors=False) -> list[str]:
    """Validate and optionally fix the structure of the given (array) dataset.

    Parameters
    ----------
        data_client : tiled.client.ArrayClient
            The data client whose structure is to be validated.
        fix_errors : bool, optional
            Whether to attempt to fix structural errors found during validation.
            Default is False.

    Returns
    -------
        list of str
            A list of human-readable notes describing any fixes applied during validation.
    """

    data_source = data_client.data_sources()[0]
    uris = [asset.data_uri for asset in data_source.assets]
    structure = data_client.structure()
    notes = []

    # Initialize adapter from uris and determine the structure as read by the adapter
    adapter_class = ADAPTERS_BY_MIMETYPE[data_source.mimetype]
    true_structure = adapter_class.from_uris(
        *uris, **data_source.parameters
    ).structure()
    true_data_type = true_structure.data_type
    true_shape = true_structure.shape
    true_chunks = true_structure.chunks

    # Validate structure components
    if structure.shape != true_shape:
        if not fix_errors:
            raise ValueError(f"Shape mismatch: {structure.shape} != {true_shape}")
        else:
            msg = f"Fixed shape mismatch: {structure.shape} -> {true_shape}"
            logger.warning(msg)
            structure.shape = true_shape
            notes.append(msg)

    if structure.chunks != true_chunks:
        if not fix_errors:
            raise ValueError(
                f"Chunk shape mismatch: {structure.chunks} != {true_chunks}"
            )
        else:
            _true_chunk_shape = tuple(c[0] for c in true_chunks)
            _chunk_shape = tuple(c[0] for c in structure.chunks)
            msg = f"Fixed chunk shape mismatch: {_chunk_shape} -> {_true_chunk_shape}"
            logger.warning(msg)
            structure.chunks = true_chunks
            notes.append(msg)

    if structure.data_type != true_data_type:
        if not fix_errors:
            raise ValueError(
                f"Data type mismatch: {structure.data_type} != {true_data_type}"
            )
        else:
            msg = f"Fixed dtype mismatch: {structure.data_type.to_numpy_dtype()} -> {true_data_type.to_numpy_dtype()}"  # noqa
            logger.warning(msg)
            structure.data_type = true_data_type
            notes.append(msg)

    if structure.dims and (len(structure.dims) != len(true_shape)):
        if not fix_errors:
            raise ValueError(
                f"Number of dimension names mismatch for a {len(true_shape)}-dimensional array: {structure.dims}"
            )  # noqa
        else:
            old_dims = structure.dims
            if len(old_dims) < len(true_shape):
                structure.dims = (
                    ("time",)
                    + old_dims
                    + tuple(
                        f"dim{i}" for i in range(len(old_dims) + 1, len(true_shape))
                    )
                )
            else:
                structure.dims = old_dims[: len(true_shape)]
            msg = f"Fixed dimension names: {old_dims} -> {structure.dims}"
            logger.warning(msg)
            notes.append(msg)

    # Update the data source structure if any fixes were applied
    if notes:
        data_source.structure = structure
        handle_error(
            data_client.context.http_client.put(
                data_client.uri.replace("/api/v1/metadata/", "/api/v1/data_source/", 1),
                content=safe_json_dump({"data_source": data_source}),
            )
        ).json()

    return notes
