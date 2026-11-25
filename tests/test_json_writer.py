import json
import os
from datetime import datetime

import numpy as np
import pytest

from bluesky_tiled_plugins.writing._json_writer import JSONLinesWriter, JSONWriter


def read_json_file(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_jsonl_file(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.parametrize(
    "writer_class, extension", [(JSONWriter, "json"), (JSONLinesWriter, "jsonl")]
)
def test_json_writer(tmpdir, writer_class, extension):
    writer = writer_class(tmpdir)
    start_doc = {"uid": "abc", "value": 1}
    event_doc = {"seq_num": 1, "data": {"x": 1}}
    stop_doc = {"exit_status": "success"}

    writer("start", start_doc)
    writer("event", event_doc)
    writer("stop", stop_doc)

    # Read the file and check its contents
    filename = os.path.join(tmpdir, f"abc.{extension}")
    data = (
        read_json_file(filename)
        if (writer_class == JSONWriter)
        else read_jsonl_file(filename)
    )
    assert data[0]["name"] == "start"
    assert data[1]["name"] == "event"
    assert data[2]["name"] == "stop"


@pytest.mark.parametrize(
    "writer_class, extension", [(JSONWriter, "json"), (JSONLinesWriter, "jsonl")]
)
def test_custom_filename(tmpdir, writer_class, extension):
    writer = writer_class(tmpdir, filename=f"custom.{extension}")
    doc = {"uid": "value"}
    writer("start", doc)
    assert os.path.exists(os.path.join(tmpdir, f"custom.{extension}"))


@pytest.mark.parametrize(
    "writer_class, extension", [(JSONWriter, "json"), (JSONLinesWriter, "jsonl")]
)
def test_non_builtin_types(tmpdir, writer_class, extension):
    """Documents may carry numpy arrays, numpy scalars and datetime objects,
    none of which the builtin `json` module can serialize.
    """
    writer = writer_class(tmpdir)
    start_doc = {
        "uid": "abc",
        "when": datetime(2026, 1, 2, 3, 4, 5),
        "motor_position": np.float64(1.5),
        "num_points": np.int64(3),
        "flag": np.bool_(True),
        "units": "\u00b5m",  # non-ASCII text must survive, as UTF-8
    }
    event_doc = {
        "seq_num": np.int64(1),
        "data": {"img": np.arange(6).reshape(2, 3), "x": np.float32(0.5)},
    }
    stop_doc = {"exit_status": "success", "num_events": {"primary": np.int64(1)}}

    writer("start", start_doc)
    writer("event", event_doc)
    writer("stop", stop_doc)

    filename = os.path.join(tmpdir, f"abc.{extension}")
    data = (
        read_json_file(filename)
        if (writer_class == JSONWriter)
        else read_jsonl_file(filename)
    )
    assert [item["name"] for item in data] == ["start", "event", "stop"]
    assert data[0]["doc"] == {
        "uid": "abc",
        "when": "2026-01-02T03:04:05",
        "motor_position": 1.5,
        "num_points": 3,
        "flag": True,
        "units": "\u00b5m",
    }
    assert data[1]["doc"] == {
        "seq_num": 1,
        "data": {"img": [[0, 1, 2], [3, 4, 5]], "x": 0.5},
    }
    assert data[2]["doc"] == {"exit_status": "success", "num_events": {"primary": 1}}
