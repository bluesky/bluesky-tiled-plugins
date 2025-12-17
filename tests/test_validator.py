import numpy as np
import pytest
from examples.render import render_templated_documents
from tiled.client.array import ArrayClient
from tiled.client.dataframe import DataFrameClient

from bluesky_tiled_plugins import TiledWriter
from bluesky_tiled_plugins.writing.validator import (
    validate_reading,
    validate_structure,
    validate,
    ReadingValidationException,
)


def test_validate_structure_shape(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with an introduced shape error
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        # Modify the document to introduce an error
        if name == "descriptor":
            doc["data_keys"]["det-key2"]["shape"] = [1, 2, 3]  # should be [1, 13, 17]

        tw(name, doc)  # Write the document

    # Get the client for the written data
    array_client = client[uid]["primary/det-key2"]
    assert isinstance(array_client, ArrayClient)

    # Try validating the structure
    with pytest.raises(ValueError, match="Shape mismatch"):
        validate_structure(array_client, fix_errors=False)

    # Now validate and fix the error
    notes = validate_structure(array_client, fix_errors=True)
    assert array_client.shape == (3, 13, 17)
    assert array_client.read().shape == (3, 13, 17)
    assert any("Fixed shape mismatch" in note for note in notes)


def test_validate_structure_chunks(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with an introduced chunk shape error
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        # Modify the document to introduce an error
        if name in {"resource", "stream_resource"}:
            doc["parameters"]["chunk_shape"] = [1, 2, 3]  # should be [3, 13, 17]

        tw(name, doc)  # Write the document

    # Get the client for the written data
    array_client = client[uid]["primary/det-key2"]
    assert isinstance(array_client, ArrayClient)

    # Try validating the structure
    with pytest.raises(ValueError, match="Chunk shape mismatch"):
        validate_structure(array_client, fix_errors=False)

    # Now validate and fix the error
    notes = validate_structure(array_client, fix_errors=True)
    assert array_client.chunks == ((3,), (13,), (17,))
    assert array_client.read() is not None
    assert any("Fixed chunk shape mismatch" in note for note in notes)


def test_validate_structure_dtype(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with an introduced dtype error
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        # Modify the document to introduce an error
        if name == "descriptor":
            doc["data_keys"]["det-key2"]["dtype_numpy"] = np.dtype(
                "int32"
            ).str  # should be "int64"

        tw(name, doc)  # Write the document

    # Get the client for the written data
    array_client = client[uid]["primary/det-key2"]
    assert isinstance(array_client, ArrayClient)

    # Try validating the structure
    with pytest.raises(ValueError, match="Data type mismatch"):
        validate_structure(array_client, fix_errors=False)

    # Now validate and fix the error
    notes = validate_structure(array_client, fix_errors=True)
    assert array_client.dtype == np.dtype("int64")
    assert array_client.read() is not None
    assert any("Fixed dtype mismatch" in note for note in notes)


def test_validate_reading_array_success(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with TiledWriter
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        tw(name, doc)  # Write the document

    # Get the client for the written data
    array_client = client[uid]["primary/det-key2"]
    assert isinstance(array_client, ArrayClient)
    assert validate_reading(array_client) is None


def test_validate_reading_array_failure(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with TiledWriter, point to non-existent file
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        # Point to a non-existent file to simulate read error
        if name == "stream_resource":
            doc["uri"] += "_non_existent"

        tw(name, doc)  # Write the document

    # Get the client for the written data
    array_client = client[uid]["primary/det-key2"]
    assert isinstance(array_client, ArrayClient)

    # Validate reading should raise an exception
    with pytest.raises(ReadingValidationException):
        validate_reading(array_client)

    # Now validate reading with ignored errors
    assert (
        validate_reading(
            array_client, ignore_errors=["Unable to synchronously open file"]
        )
        is None
    )


def test_validate_reading_table_success(client):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with TiledWriter
    documents = render_templated_documents("internal_events.json", "")
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        tw(name, doc)  # Write the document

    # Get the client for the written data
    table_client = client[uid]["primary"].base["internal"]
    assert isinstance(table_client, DataFrameClient)
    assert validate_reading(table_client) is None


def test_validate_bluesky_run_success(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with TiledWriter
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        tw(name, doc)  # Write the document

    # Run the full validation
    assert validate(client[uid]) is True

    # There should be no validation notes since everything is correct
    assert client[uid].metadata.get("notes") is None


def test_validate_bluesky_run_failure(client, external_assets_folder):
    tw = TiledWriter(client, validate=False)  # Do not validate on write (default)

    # Write documents with an introduced shape error
    documents = render_templated_documents(
        "external_assets_single_key.json", external_assets_folder
    )
    for item in documents:
        name, doc = item["name"], item["doc"]
        if name == "start":
            uid = doc["uid"]

        # Modify the document to introduce an error
        if name == "descriptor":
            doc["data_keys"]["det-key2"]["shape"] = [1, 2, 3]  # should be [1, 13, 17]

        tw(name, doc)  # Write the document

    # Run the full validation, which should fail/raise
    with pytest.raises(ValueError, match="Shape mismatch"):
        validate(client[uid], fix_errors=False, raise_on_error=True)

    assert validate(client[uid], fix_errors=False, raise_on_error=False) is False

    # Now run validation with fixing enabled
    assert validate(client[uid], fix_errors=True) is True

    # There should be validation notes about the fixes applied
    notes = client[uid].metadata.get("notes", [])
    assert any("Fixed shape mismatch" in note for note in notes)
