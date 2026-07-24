from math import ceil

import pytest

from bluesky_tiled_plugins.writing.consolidators import (
    BytesConsolidator,
    HDF5Consolidator,
    Patch,
    consolidator_factory,
)
from tiled.structures.bytes import BytesStructure
from tiled.structures.core import StructureFamily
from tiled.structures.data_source import Management


# Most fixtures in this module do not set join_method explicitly and therefore rely on
# the default, which currently triggers a DeprecationWarning announcing the upcoming
# change of the default to "stack". Silence it at the module level so the strict
# filterwarnings=["error"] policy does not turn expected usage into errors.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*default value of join_method will change.*:DeprecationWarning"
)


@pytest.fixture
def descriptor():
    return {
        "data_keys": {
            "test_img": {
                "shape": [1, 10, 15],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_7_imgs": {
                "shape": [7, 10, 15],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_6_imgs": {
                "shape": [6, 10, 15],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_cube": {
                "shape": [1, 10, 15, 3],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_7_cubes": {
                "shape": [7, 10, 15, 3],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_arr": {
                "shape": [
                    1,
                    3,
                ],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_7_arrs": {
                "shape": [
                    7,
                    3,
                ],
                "dtype": "array",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_num": {
                "shape": [
                    1,
                ],
                "dtype": "number",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
            "test_7_nums": {
                "shape": [
                    7,
                ],
                "dtype": "number",
                "dtype_numpy": "<f8",
                "external": "STREAM:",
                "object_name": "test_object",
            },
        },
        "uid": "descriptor-uid",
    }


@pytest.fixture
def hdf5_stream_resource_factory():
    def _make(data_key, chunk_shape, spec=None):
        parameters = {
            "dataset": f"entry/data/{data_key}",
            "swmr": True,
            "chunk_shape": chunk_shape,
        }
        if spec is not None:
            parameters["spec"] = spec
        return {
            "data_key": data_key,
            "mimetype": "application/x-hdf5",
            "uri": "file://localhost/test/file/path",
            "resource_path": "test_file.h5",
            "parameters": parameters,
            "uid": f"stream-resource-uid-{data_key}",
        }

    return _make


@pytest.fixture
def image_seq_stream_resource_factory():
    format_to_mimetype = {
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "tiff": "image/tiff",
        "tif": "image/tiff",
    }
    return lambda image_format, data_key, chunk_shape: {
        "data_key": data_key,
        "mimetype": f"multipart/related;type={format_to_mimetype[image_format]}",
        "uri": "file://localhost/test/file/path",
        "parameters": {
            "chunk_shape": chunk_shape,
            "template": "img_{:06d}." + image_format,
        },
        "uid": f"stream-resource-uid-{data_key}",
    }


@pytest.fixture
def csv_stream_resource_factory():
    return lambda data_key, chunk_shape: {
        "data_key": data_key,
        "mimetype": "text/csv;header=absent",
        "uri": "file://localhost/test/file/path",
        "resource_path": "test_file.csv",
        "parameters": {"chunk_shape": chunk_shape},
        "uid": f"stream-resource-uid-{data_key}",
    }


@pytest.fixture
def stream_datum_factory():
    return lambda data_key, indx, i_start, i_stop: {
        "seq_nums": {"start": i_start + 1, "stop": i_stop + 1},
        "indices": {"start": i_start, "stop": i_stop},
        "descriptor": "descriptor-uid",
        "stream_resource": f"stream-resource-uid-{data_key}",
        "uid": f"stream-datum-uid-{data_key}/{indx}",
    }


@pytest.fixture
def bytes_stream_resource_factory():
    def _make(data_key, template=None, filename="", spec=None):
        parameters: dict = {}
        if template is not None:
            parameters["template"] = template
        if filename:
            parameters["filename"] = filename
        if spec is not None:
            parameters["spec"] = spec
        return {
            "data_key": data_key,
            "mimetype": "application/octet-stream",
            "uri": "file://localhost/test/file/path/"
            if template is not None
            else "file://localhost/test/file/path/blob.bin",
            "parameters": parameters,
            "uid": f"stream-resource-uid-{data_key}",
        }

    return _make


# Tuples of (data_key, frames_per_datum, join_method, expected_shape)
shape_testdata = [
    # 5 events, 1 or 7 image per event, 10x15 pixels
    ("test_img", 1, "concat", (5, 10, 15)),
    ("test_7_imgs", 7, "concat", (35, 10, 15)),
    ("test_img", 1, "stack", (5, 1, 10, 15)),
    ("test_7_imgs", 7, "stack", (5, 7, 10, 15)),
    # 5 events, 1 or 7 cube per event, 10x15x3 pixels
    ("test_cube", 1, "concat", (5, 10, 15, 3)),
    ("test_7_cubes", 7, "concat", (35, 10, 15, 3)),
    ("test_cube", 1, "stack", (5, 1, 10, 15, 3)),
    ("test_7_cubes", 7, "stack", (5, 7, 10, 15, 3)),
    # 5 events, 1 or 7 array per event, 1 element in array
    ("test_arr", 1, "concat", (5, 3)),
    ("test_7_arrs", 7, "concat", (35, 3)),
    ("test_arr", 1, "stack", (5, 1, 3)),
    ("test_7_arrs", 7, "stack", (5, 7, 3)),
    # 5 events, 1 or 7 number per event
    ("test_num", 1, "concat", (5,)),
    ("test_7_nums", 7, "concat", (35,)),
    ("test_num", 1, "stack", (5, 1)),
    ("test_7_nums", 7, "stack", (5, 7)),
]


@pytest.mark.parametrize(
    "data_key, frames_per_datum, join_method, expected", shape_testdata
)
def test_hdf5_shape(
    descriptor,
    hdf5_stream_resource_factory,
    stream_datum_factory,
    data_key,
    frames_per_datum,
    join_method,
    expected,
):
    stream_resource = hdf5_stream_resource_factory(data_key=data_key, chunk_shape=())
    cons = HDF5Consolidator(stream_resource, descriptor)
    cons.join_method = join_method
    assert cons.shape == (0, *expected[1:])
    for i in range(5):
        doc = stream_datum_factory(data_key, i, i, i + 1)
        cons.consume_stream_datum(doc)
    assert cons.shape == expected


supported_image_seq_formats = ["jpeg", "tiff", "jpg", "tif"]


@pytest.mark.parametrize(
    "data_key, frames_per_datum, join_method, expected", shape_testdata
)
@pytest.mark.parametrize("image_format", supported_image_seq_formats)
@pytest.mark.parametrize("indx_per_stream_datum_doc", [1, 2, 3, 5])
def test_tiff_and_jpeg_shape(
    descriptor,
    image_seq_stream_resource_factory,
    stream_datum_factory,
    image_format,
    data_key,
    frames_per_datum,
    join_method,
    expected,
    indx_per_stream_datum_doc,
):
    stream_resource = image_seq_stream_resource_factory(
        image_format=image_format, data_key=data_key, chunk_shape=(1,)
    )
    cons = consolidator_factory(stream_resource, descriptor)
    cons.join_method = join_method
    assert cons.shape == (0, *expected[1:])
    for i in range(ceil(5 / indx_per_stream_datum_doc)):
        doc = stream_datum_factory(
            data_key,
            i,
            i * indx_per_stream_datum_doc,
            min((i + 1) * indx_per_stream_datum_doc, 5),
        )
        cons.consume_stream_datum(doc)
    assert cons.shape == expected

    # Stackable case here corresponds to multipage tiffs (AD does not support them though)
    assert len(cons.assets) == 5 * frames_per_datum if join_method == "concat" else 5


# Tuples of (data_key, chunk_shape, expected_shape, expected_chunks)
csv_testdata = [
    # 5 events, 1 or 7 array per event, 1 element in array
    ("test_arr", (1,), (5, 3), ((1,) * 5, (3,))),
    ("test_7_arrs", (1,), (35, 3), ((1,) * 35, (3,))),
    ("test_arr", (), (5, 3), ((5,), (3,))),
    ("test_7_arrs", (), (35, 3), ((35,), (3,))),
    ("test_arr", (10,), (5, 3), ((1,) * 5, (3,))),
    ("test_7_arrs", (10,), (35, 3), ((7, 7, 7, 7, 7), (3,))),
    ("test_7_arrs", (3,), (35, 3), ((3, 3, 1) * 5, (3,))),
]


@pytest.mark.parametrize(
    "data_key, chunk_shape, expected_shape, expected_chunks", csv_testdata
)
def test_csv_shape_and_chunks(
    descriptor,
    csv_stream_resource_factory,
    stream_datum_factory,
    data_key,
    chunk_shape,
    expected_shape,
    expected_chunks,
):
    stream_resource = csv_stream_resource_factory(
        data_key=data_key, chunk_shape=chunk_shape
    )
    cons = consolidator_factory(stream_resource, descriptor)
    assert cons.join_method == "concat"
    assert not cons.join_chunks
    assert cons.shape == (0, *expected_shape[1:])
    for i in range(5):
        doc = stream_datum_factory(data_key, i, i, i + 1)
        cons.consume_stream_datum(doc)
    assert cons.shape == expected_shape
    assert cons.chunks == expected_chunks


# Tuples of (data_key, join_method, join_chunks, chunk_shape, expected_chunks)
chunk_hdf5_testdata = [
    ("test_img", "stack", True, (), ((5,), (1,), (10,), (15,))),
    ("test_img", "stack", True, (1, 1, 10, 15), ((1, 1, 1, 1, 1), (1,), (10,), (15,))),
    ("test_img", "stack", True, (2,), ((2, 2, 1), (1,), (10,), (15,))),
    ("test_img", "stack", True, (5, 1, 10, 15), ((5,), (1,), (10,), (15,))),
    ("test_img", "stack", True, (10, 1), ((5,), (1,), (10,), (15,))),
    ("test_img", "stack", True, (3, 1, 4, 5), ((3, 2), (1,), (4, 4, 2), (5, 5, 5))),
    ("test_7_imgs", "stack", True, (), ((5,), (7,), (10,), (15,))),
    (
        "test_7_imgs",
        "stack",
        True,
        (1, 1),
        ((1, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1, 1), (10,), (15,)),
    ),
    ("test_7_imgs", "stack", True, (2,), ((2, 2, 1), (7,), (10,), (15,))),
    (
        "test_7_imgs",
        "stack",
        True,
        (5, 1, 10, 15),
        ((5,), (1, 1, 1, 1, 1, 1, 1), (10,), (15,)),
    ),
    ("test_7_imgs", "stack", True, (10, 5), ((5,), (5, 2), (10,), (15,))),
    (
        "test_7_imgs",
        "stack",
        True,
        (3, 4, 5, 6),
        (
            (3, 2),
            (4, 3),
            (5, 5),
            (6, 6, 3),
        ),
    ),
    (
        "test_cube",
        "stack",
        True,
        (3, 1, 4, 5, 3),
        ((3, 2), (1,), (4, 4, 2), (5, 5, 5), (3,)),
    ),
    (
        "test_7_cubes",
        "stack",
        True,
        (3, 4, 5, 6, 7),
        ((3, 2), (4, 3), (5, 5), (6, 6, 3), (3,)),
    ),
    ("test_arr", "stack", True, (5, 1, 1), ((5,), (1,), (1, 1, 1))),
    ("test_arr", "stack", True, (2,), ((2, 2, 1), (1,), (3,))),
    ("test_7_arrs", "stack", True, (5, 1, 1), ((5,), (1, 1, 1, 1, 1, 1, 1), (1, 1, 1))),
    ("test_7_arrs", "stack", True, (2,), ((2, 2, 1), (7,), (3,))),
    ("test_num", "stack", True, (), ((5,), (1,))),
    ("test_num", "stack", True, (2,), ((2, 2, 1), (1,))),
    ("test_7_nums", "stack", True, (), ((5,), (7,))),
    ("test_7_nums", "stack", True, (2,), ((2, 2, 1), (7,))),
    ("test_7_nums", "stack", True, (2, 3), ((2, 2, 1), (3, 3, 1))),
    ("test_img", "concat", True, (), ((5,), (10,), (15,))),
    ("test_img", "concat", True, (1, 10, 15), ((1, 1, 1, 1, 1), (10,), (15,))),
    ("test_img", "concat", True, (2,), ((2, 2, 1), (10,), (15,))),
    ("test_img", "concat", True, (5, 10, 15), ((5,), (10,), (15,))),
    ("test_img", "concat", True, (10, 1), ((5,), (1,) * 10, (15,))),
    ("test_img", "concat", True, (3, 4, 5), ((3, 2), (4, 4, 2), (5, 5, 5))),
    ("test_7_imgs", "concat", True, (), ((35,), (10,), (15,))),
    ("test_7_imgs", "concat", False, (), ((35,), (10,), (15,))),
    ("test_7_imgs", "concat", True, (1, 1), ((1,) * 35, (1,) * 10, (15,))),
    ("test_7_imgs", "concat", False, (2,), ((2, 2, 2, 1) * 5, (10,), (15,))),
    ("test_7_imgs", "concat", True, (2,), ((2,) * 17 + (1,), (10,), (15,))),
    ("test_7_imgs", "concat", True, (5, 10, 15), ((5,) * 7, (10,), (15,))),
    ("test_7_imgs", "concat", False, (5, 10, 15), ((5, 2) * 5, (10,), (15,))),
    ("test_7_imgs", "concat", True, (10, 5), ((10, 10, 10, 5), (5, 5), (15,))),
    ("test_7_imgs", "concat", False, (10, 5), ((7,) * 5, (5, 5), (15,))),
    (
        "test_7_imgs",
        "concat",
        True,
        (3, 5, 6),
        (
            (3,) * 11 + (2,),
            (5, 5),
            (6, 6, 3),
        ),
    ),
    (
        "test_7_imgs",
        "concat",
        False,
        (3, 5, 6),
        (
            (3, 3, 1) * 5,
            (5, 5),
            (6, 6, 3),
        ),
    ),
    ("test_cube", "concat", True, (3, 4, 5, 3), ((3, 2), (4, 4, 2), (5, 5, 5), (3,))),
    (
        "test_7_cubes",
        "concat",
        True,
        (3, 5, 6, 7),
        ((3,) * 11 + (2,), (5, 5), (6, 6, 3), (3,)),
    ),
    (
        "test_7_cubes",
        "concat",
        False,
        (3, 5, 6, 7),
        ((3, 3, 1) * 5, (5, 5), (6, 6, 3), (3,)),
    ),
    ("test_arr", "concat", True, (5, 1), ((5,), (1, 1, 1))),
    ("test_arr", "concat", True, (2,), ((2, 2, 1), (3,))),
    ("test_7_arrs", "concat", True, (5, 1), ((5,) * 7, (1, 1, 1))),
    ("test_7_arrs", "concat", False, (5, 1), ((5, 2) * 5, (1, 1, 1))),
    ("test_7_arrs", "concat", True, (2,), ((2,) * 17 + (1,), (3,))),
    ("test_7_arrs", "concat", False, (2,), ((2, 2, 2, 1) * 5, (3,))),
    ("test_num", "concat", True, (), ((5,),)),
    ("test_num", "concat", True, (2,), ((2, 2, 1),)),
    ("test_7_nums", "concat", True, (), ((35,),)),
    ("test_7_nums", "concat", False, (), ((35,),)),
    ("test_7_nums", "concat", True, (3,), ((3,) * 11 + (2,),)),
    ("test_7_nums", "concat", False, (3,), ((3, 3, 1) * 5,)),
]


@pytest.mark.parametrize(
    "data_key, join_method, join_chunks, chunk_shape, expected", chunk_hdf5_testdata
)
def test_hdf5_chunks(
    descriptor,
    hdf5_stream_resource_factory,
    stream_datum_factory,
    data_key,
    join_method,
    join_chunks,
    chunk_shape,
    expected,
):
    stream_resource = hdf5_stream_resource_factory(
        data_key=data_key, chunk_shape=chunk_shape
    )
    cons = HDF5Consolidator(stream_resource, descriptor)
    cons.join_method = join_method
    cons.join_chunks = join_chunks
    assert cons.chunks == ((0,), *expected[1:])
    for i in range(5):
        doc = stream_datum_factory(data_key, i, i, i + 1)
        cons.consume_stream_datum(doc)
    assert cons.chunks == expected


# Tuples of (data_key, join_method, join_chunks, frames_per_datum, indx_per_stream_datum_doc, chunk_shape, expected_chunks)
chunk_tiff_testdata = [
    ("test_img", "stack", True, 1, 1, (1,), ((1, 1, 1, 1, 1), (1,), (10,), (15,))),
    ("test_img", "stack", True, 1, 2, (1,), ((1, 1, 1, 1, 1), (1,), (10,), (15,))),
    ("test_img", "stack", True, 1, 5, (1,), ((1, 1, 1, 1, 1), (1,), (10,), (15,))),
    ("test_6_imgs", "stack", True, 6, 1, (1,), ((1,) * 5, (6,), (10,), (15,))),
    ("test_6_imgs", "concat", True, 6, 1, (1,), ((1,) * 30, (10,), (15,))),
    ("test_6_imgs", "stack", True, 6, 2, (2,), ((2, 2, 1), (6,), (10,), (15,))),
    ("test_6_imgs", "concat", True, 6, 2, (2,), ((2,) * 15, (10,), (15,))),
    ("test_6_imgs", "stack", True, 6, 4, (3,), ((3, 2), (6,), (10,), (15,))),
    ("test_6_imgs", "concat", True, 6, 4, (3,), ((3,) * 10, (10,), (15,))),
]


@pytest.mark.parametrize(
    "data_key, join_method, join_chunks, frames_per_datum, indx_per_stream_datum_doc, chunk_shape, expected_chunks",  # noqa
    chunk_tiff_testdata,
)
@pytest.mark.parametrize("image_format", supported_image_seq_formats)
def test_tiff_and_jpeg_chunks(
    descriptor,
    image_seq_stream_resource_factory,
    stream_datum_factory,
    image_format,
    data_key,
    join_method,
    join_chunks,
    frames_per_datum,
    indx_per_stream_datum_doc,
    chunk_shape,
    expected_chunks,
):
    """Test the chunking of (possibly multipage) tiff and jpeg datasets and the number of registered files."""

    stream_resource = image_seq_stream_resource_factory(
        image_format=image_format, data_key=data_key, chunk_shape=chunk_shape
    )
    cons = consolidator_factory(stream_resource, descriptor)
    cons.join_method = join_method
    cons.join_chunks = join_chunks
    assert cons.chunks == ((0,), *expected_chunks[1:])
    for i in range(ceil(5 / indx_per_stream_datum_doc)):
        doc = stream_datum_factory(
            data_key,
            i,
            i * indx_per_stream_datum_doc,
            min((i + 1) * indx_per_stream_datum_doc, 5),
        )
        cons.consume_stream_datum(doc)
    assert cons.chunks == expected_chunks

    # Check the number of registered files
    assert (
        len(cons.assets) == 5 * frames_per_datum / expected_chunks[0][0]
        if join_method == "concat"
        else 5
    )


@pytest.mark.parametrize("chunk_shape", [(5,), (10,)])
def test_multipart_related_coerces_non_divisor_chunk_shape(
    descriptor, image_seq_stream_resource_factory, chunk_shape
):
    """When frames-per-file does not divide frames-per-datum under `concat`,
    `MultipartRelatedConsolidator` warns and coerces chunk_shape to one file per datum."""
    sres = image_seq_stream_resource_factory(
        image_format="tiff", data_key="test_6_imgs", chunk_shape=chunk_shape
    )
    with pytest.warns(UserWarning, match="does not divide"):
        cons = consolidator_factory(sres, descriptor)
    assert cons.chunk_shape == (6,)
    assert cons.files_per_datum == 1


def test_multipart_related_files_per_datum_override(
    descriptor, image_seq_stream_resource_factory, stream_datum_factory
):
    """The `files_per_datum` StreamResource parameter overrides the inferred value,
    both at construction time and after `update_from_stream_resource`."""
    sres = image_seq_stream_resource_factory(
        image_format="tiff", data_key="test_6_imgs", chunk_shape=(1,)
    )
    sres["parameters"]["files_per_datum"] = 3
    cons = consolidator_factory(sres, descriptor)
    assert cons.files_per_datum == 3

    # Two datums (indices 0..1) with files_per_datum=3 -> 6 files registered.
    cons.consume_stream_datum(stream_datum_factory("test_6_imgs", 0, 0, 2))
    assert len(cons.assets) == 6

    # A subsequent StreamResource without files_per_datum falls back to the
    # inferred value (6 frames / chunk_shape[0]=1 => 6).
    sres2 = image_seq_stream_resource_factory(
        image_format="tiff", data_key="test_6_imgs", chunk_shape=(1,)
    )
    cons.update_from_stream_resource(sres2)
    assert cons.files_per_datum == 6

    # And overriding again on the next update is honored.
    sres3 = image_seq_stream_resource_factory(
        image_format="tiff", data_key="test_6_imgs", chunk_shape=(1,)
    )
    sres3["parameters"]["files_per_datum"] = 2
    cons.update_from_stream_resource(sres3)
    assert cons.files_per_datum == 2


# Tuples of (filename, original_template, expected_template, formatted)
template_testdata = [
    ("", "img_{:06d}", "img_{:06d}", "img_000042"),
    ("img", "{:s}_{:06d}", "img_{:06d}", "img_000042"),
    ("img", "%s_%06d", "img_{:06d}", "img_000042"),
    ("", "img%s_%06d", "img_{:06d}", "img_000042"),
    ("img", "%s_%1d", "img_{:1d}", "img_42"),
    ("img", "%s_%-6d", "img_{:<6d}", "img_42    "),
    ("img", "%s_%+06d", "img_{:+06d}", "img_+00042"),
    ("img", "%s_% 06d", "img_{: 06d}", "img_ 00042"),
    ("img", "%s_%-+6d", "img_{:<+6d}", "img_+42   "),
    ("img", "%s_%- 6d", "img_{:< 6d}", "img_ 42   "),
    ("img", "%s_%6.6d", "img_{:06d}", "img_000042"),
    # Width lexicographically less than precision must still take the numeric max (10, not "9").
    ("img", "%s_%9.10d", "img_{:010d}", "img_0000000042"),
]


@pytest.mark.parametrize("image_format", supported_image_seq_formats)
@pytest.mark.parametrize(
    "filename, original_template, expected_template, formatted", template_testdata
)
def test_name_templating(
    descriptor,
    image_seq_stream_resource_factory,
    image_format,
    filename,
    original_template,
    expected_template,
    formatted,
):
    stream_resource = image_seq_stream_resource_factory(
        image_format=image_format, data_key="test_img", chunk_shape=((1),)
    )
    stream_resource["parameters"]["template"] = f"{original_template}.{image_format}"
    if filename:
        stream_resource["parameters"]["filename"] = filename
    cons = consolidator_factory(stream_resource, descriptor)
    assert cons.template == f"{expected_template}.{image_format}"
    assert cons.template.format(42) == f"{formatted}.{image_format}"


@pytest.mark.parametrize(
    "patches, expected_shape, expected_offset",
    [
        # Single patch
        ([Patch(shape=(5, 10), offset=(0, 0))], (5, 10), (0, 0)),
        # Two patches, non-overlapping
        (
            [Patch(shape=(5, 10), offset=(0, 0)), Patch(shape=(3, 7), offset=(5, 10))],
            (8, 17),
            (0, 0),
        ),
        # Two patches, overlapping
        (
            [Patch(shape=(5, 10), offset=(0, 0)), Patch(shape=(3, 7), offset=(2, 5))],
            (5, 12),
            (0, 0),
        ),
        # Multiple patches, scattered
        (
            [
                Patch(shape=(2, 2), offset=(1, 1)),
                Patch(shape=(3, 1), offset=(0, 3)),
                Patch(shape=(1, 4), offset=(2, 0)),
            ],
            (3, 4),
            (0, 0),
        ),
        # Multiple patches, disjoint
        (
            [
                Patch(shape=(2, 2), offset=(0, 0)),
                Patch(shape=(3, 1), offset=(5, 5)),
                Patch(shape=(1, 4), offset=(10, 10)),
            ],
            (11, 14),
            (0, 0),
        ),
        # 1D patches
        (
            [
                Patch(shape=(3,), offset=(2,)),
                Patch(shape=(2,), offset=(0,)),
                Patch(shape=(1,), offset=(5,)),
            ],
            (6,),
            (0,),
        ),
    ],
)
def test_combine_patches(patches, expected_shape, expected_offset):
    combined = Patch.combine_patches(patches)
    assert combined.shape == expected_shape
    assert combined.offset == expected_offset


# Tuples of (template, filename, expected_template, ranges, expected_suffixes)
# where `ranges` is a list of (start, stop) index pairs for successive
# stream_datum documents, and `expected_suffixes` is the URI suffix each
# resulting Asset should end with.
bytes_asset_testdata = [
    # No template: one asset reusing the base URI.
    (None, "", None, [(0, 1)], ["blob.bin"]),
    # New-style template, two batches.
    (
        "blob_{:05d}.bin",
        "",
        "blob_{:05d}.bin",
        [(0, 3), (3, 5)],
        [f"blob_{i:05d}.bin" for i in range(5)],
    ),
    # Legacy `%s_%06d` template with filename prefix.
    (
        "%s_%06d.bin",
        "scan",
        "scan_{:06d}.bin",
        [(0, 1)],
        ["scan_000000.bin"],
    ),
]


@pytest.mark.parametrize(
    "template, filename, expected_template, ranges, expected_suffixes",
    bytes_asset_testdata,
)
def test_bytes_asset_generation(
    descriptor,
    bytes_stream_resource_factory,
    template,
    filename,
    expected_template,
    ranges,
    expected_suffixes,
):
    """`BytesConsolidator` registers one Asset per file index, honoring the
    (optional) filename template, and produces a `bytes` DataSource."""
    sres = bytes_stream_resource_factory(
        data_key="test_img", template=template, filename=filename
    )
    cons = BytesConsolidator(sres, descriptor)
    assert cons.template == expected_template
    assert cons.assets == []

    for i, (start, stop) in enumerate(ranges):
        patch = cons.consume_stream_datum({"indices": {"start": start, "stop": stop}})
        assert patch == Patch(offset=(start,), shape=(stop - start,))

    uris = [a.data_uri for a in cons.assets]
    assert [u.split("/")[-1] for u in uris] == expected_suffixes
    assert [a.num for a in cons.assets] == list(range(len(cons.assets)))
    assert all(a.parameter == "data_uris" for a in cons.assets)
    assert all(a.is_directory is False for a in cons.assets)

    ds = cons.get_data_source()
    assert ds.structure_family == StructureFamily.bytes
    assert isinstance(ds.structure, BytesStructure)
    assert ds.mimetype == "application/octet-stream"
    assert ds.management == Management.external
    assert ds.parameters == {} and ds.properties == {}
    assert len(ds.assets) == len(expected_suffixes)


def test_bytes_update_from_stream_resource_resets_index_offset(
    descriptor, bytes_stream_resource_factory
):
    """A second StreamResource restarts the template index at 0 relative to
    the new batch, so files land at the correct name."""
    sres1 = bytes_stream_resource_factory(data_key="test_img", template="a_{:03d}.bin")
    cons = BytesConsolidator(sres1, descriptor)
    cons.consume_stream_datum({"indices": {"start": 0, "stop": 2}})
    assert cons.assets[-1].data_uri.endswith("a_001.bin")

    sres2 = bytes_stream_resource_factory(data_key="test_img", template="b_{:03d}.bin")
    cons.update_from_stream_resource(sres2)
    assert cons.template == "b_{:03d}.bin"
    cons.consume_stream_datum({"indices": {"start": 2, "stop": 4}})
    # Second batch starts at "0" for the new template, not at "2".
    assert cons.assets[2].data_uri.endswith("b_000.bin")
    assert cons.assets[3].data_uri.endswith("b_001.bin")


@pytest.mark.parametrize(
    "spec, expected_metadata",
    [(None, {}), ("MY_SPEC", {"spec": "MY_SPEC"})],
)
@pytest.mark.parametrize(
    "cons_cls, sres_factory_name, factory_kwargs",
    [
        (BytesConsolidator, "bytes_stream_resource_factory", {}),
        (HDF5Consolidator, "hdf5_stream_resource_factory", {"chunk_shape": ()}),
    ],
    ids=["bytes", "hdf5"],
)
def test_spec_metadata_propagation(
    request,
    descriptor,
    cons_cls,
    sres_factory_name,
    factory_kwargs,
    spec,
    expected_metadata,
):
    """The `spec` StreamResource parameter is propagated to consolidator metadata
    the same way for `BytesConsolidator` and (any subclass of) `ConsolidatorBase`."""
    sres_factory = request.getfixturevalue(sres_factory_name)
    sres = sres_factory(data_key="test_img", spec=spec, **factory_kwargs)
    cons = cons_cls(sres, descriptor)
    assert cons.metadata == expected_metadata


def test_bytes_factory_validate_and_mimetype_guard(
    descriptor, bytes_stream_resource_factory
):
    """`consolidator_factory` dispatches to `BytesConsolidator`, `validate`
    succeeds when assets are reachable, and an unsupported mimetype is rejected."""
    sres = bytes_stream_resource_factory(data_key="test_img")
    cons = consolidator_factory(sres, descriptor)
    assert isinstance(cons, BytesConsolidator)
    assert cons.validate() == []
    assert cons.validate(fix_errors=True) == []

    sres["mimetype"] = "application/x-hdf5"
    with pytest.raises(ValueError, match="can not be handled by BytesConsolidator"):
        BytesConsolidator(sres, descriptor)


# Tuples of (uri, template, expected_uri).
uri_join_testdata = [
    # Directory URI + plain filename template.
    (
        "file://localhost/data/detector",
        "/img_{:06d}.jpg",
        "file://localhost/data/detector/img_000000.jpg",
    ),
    # Filename-prefix URI + suffix template (ophyd Registry style).
    (
        "file://localhost/data/detector/51ea13ff",
        "_{:06d}.jpg",
        "file://localhost/data/detector/51ea13ff_000000.jpg",
    ),
    # Both sides carry a `/` -- the degenerate `//` at the junction is
    # collapsed to a single `/`.
    (
        "file://localhost/data/detector/",
        "/img_{:06d}.jpg",
        "file://localhost/data/detector/img_000000.jpg",
    ),
    # Non-`file://` scheme is treated identically -- no filesystem probing.
    (
        "https://example.invalid/data/detector/",
        "/img_{:06d}.jpg",
        "https://example.invalid/data/detector/img_000000.jpg",
    ),
]


@pytest.mark.parametrize("uri, template, expected_uri", uri_join_testdata)
def test_get_datum_uri_join(
    descriptor, image_seq_stream_resource_factory, uri, template, expected_uri
):
    """`get_datum_uri` concatenates URI and rendered template deterministically.

    The join is purely syntactic (no filesystem probing) so it works for
    remote URIs and for data that has not yet been flushed to disk. Both
    conventions in the wild are preserved: filename-prefix URI + suffix
    template, and directory URI + slash-prefixed filename template. A
    single degenerate `//` at the junction is collapsed to `/`.
    """
    stream_resource = image_seq_stream_resource_factory(
        image_format="jpeg", data_key="test_img", chunk_shape=(1,)
    )
    stream_resource["uri"] = uri
    stream_resource["parameters"]["template"] = template
    cons = consolidator_factory(stream_resource, descriptor)
    assert cons.get_datum_uri(0) == expected_uri
