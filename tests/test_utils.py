import math

import orjson
import pyarrow
import pytest

from bluesky_tiled_plugins.utils import split_table, truncate_json_overflow


def test_truncate_json_overflow():
    # Test with a large integer
    data = {"large_pos_int": 2**60, "large_neg_int": -(2**60)}
    truncated_data = truncate_json_overflow(data)
    assert orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)
    for val in orjson.loads(
        orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)
    ).values():
        assert val is not None

    # Test with a large float
    data = {"large_pos_float": 2e308, "large_neg_float": -2e308}
    truncated_data = truncate_json_overflow(data)
    assert orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)
    for val in orjson.loads(
        orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)
    ).values():
        assert val is not None

    # Test with a list of large integers and floats
    data = [[2**60, -(2**60)], [2e308, -2e308]]
    truncated_data = truncate_json_overflow(data)
    assert orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)

    # Test with a dictionary containing various types
    data = {
        "int": 42,
        "float": 3.14,
        "str": "Hello, world!",
        "list": [1, 2, 3],
        "dict": {"key": "value"},
        "large_int": 2**60,
        "large_float": 2e308,
        "nested": {
            "large_neg_int": -(2**60),
            "large_neg_float": -2e308,
            "list_of_large_ints": [2**60, -(2**60)],
            "list_of_large_floats": [2e308, -2e308],
        },
    }
    truncated_data = truncate_json_overflow(data)
    assert orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER)

    # Test with a NaN value
    data = {"nan": float("nan")}
    truncated_data = truncate_json_overflow(data)
    assert (
        orjson.loads(orjson.dumps(truncated_data, option=orjson.OPT_STRICT_INTEGER))[
            "nan"
        ]
        is None
    )


@pytest.mark.parametrize(
    "ncols, max_columns",
    [
        (10, 4),  # remainder: parts of unequal size
        (9, 3),  # exact division
        (5, 5),  # single part (ncols == max_columns)
        (1, 4),  # single column, single part
        (7, 1),  # one column per part
    ],
)
def test_split_table(ncols, max_columns):
    table = pyarrow.table({f"col_{i:03d}": [i, i + 1] for i in range(ncols)})

    parts = list(split_table(table, max_columns))

    # The table is split into the minimal number of balanced parts, none of
    # which exceeds `max_columns` columns.
    assert len(parts) == math.ceil(ncols / max_columns)
    assert all(part.num_columns <= max_columns for part in parts)

    # Every column appears exactly once across the parts, sorted by name, and
    # the underlying data is preserved.
    recombined = [name for part in parts for name in part.column_names]
    assert recombined == sorted(table.column_names)
    for part in parts:
        for name in part.column_names:
            assert part.column(name).to_pylist() == table.column(name).to_pylist()
