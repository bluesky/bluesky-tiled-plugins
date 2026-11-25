from datetime import datetime
from pathlib import Path
from typing import Optional

from tiled.utils import safe_json_dump

# NOTE: This code is duplicated in src/bluesky/callbacks/json_writer.py
#
# Documents are serialized with `tiled.utils.safe_json_dump`, which accepts
# types that the builtin `json` module rejects (numpy arrays and scalars,
# datetime objects, bytes, paths). It returns UTF-8 encoded bytes, so the
# files are opened in binary mode.


def default_name(name, doc, suffix=""):
    """Default naming function for JSON runs.

    If the first document is a start document, use the first part of uid to create
    a filename. Otherwise, use the current timestamp.
    """

    if name == "start":
        return f"{doc['uid'].split('-')[0]}{suffix}"
    else:
        return f"{datetime.today().strftime('%Y-%m-%d_%H-%M-%S')}{suffix}"


class JSONWriter:
    """Writer of Bluesky documents of a single run into a JSON file as an array.

    The file is created when a Start document is received, each new document is
    written immediately, and the JSON array is closed when the "stop" document
    is received.
    """

    def __init__(
        self,
        dirname: str,
        filename: str | None = None,
    ):
        self.dirname = Path(dirname)
        self.dirname.mkdir(parents=True, exist_ok=True)
        self.filename = filename

    def __call__(self, name, doc):
        self.filename = self.filename or default_name(name, doc, suffix=".json")
        record = safe_json_dump({"name": name, "doc": doc})

        if name == "start":
            with open(self.dirname / self.filename, "wb") as file:
                file.write(b"[\n" + record + b",\n")

        elif name == "stop":
            with open(self.dirname / self.filename, "ab") as file:
                file.write(record + b"\n]")

        else:
            with open(self.dirname / self.filename, "ab") as file:
                file.write(record + b",\n")


class JSONLinesWriter:
    """Writer of Bluesky documents from a single run into a JSON Lines file

    If the file already exists, new documents will be appended to it.
    """

    def __init__(self, dirname: str, filename: str | None = None):
        self.dirname = Path(dirname)
        self.dirname.mkdir(parents=True, exist_ok=True)
        self.filename = filename

    def __call__(self, name, doc):
        self.filename = self.filename or default_name(name, doc, suffix=".jsonl")
        record = safe_json_dump({"name": name, "doc": doc})

        with open(self.dirname / self.filename, "ab") as file:
            file.write(record + b"\n")


class JSONDictWriter:
    """Writer of Bluesky documents from a single run into a dictionary in JSON format

    The dictionary can be in memory or backed by Redis or any other key-value store. The writer
    will append documents to a list under the key corresponding to the document uuid.
    """

    def __init__(self, store: dict, key: Optional[str] = None):
        self.store = store
        self.key = key

    def __call__(self, name, doc):
        self.key = self.key or default_name(name, doc)

        if self.key not in self.store:
            self.store[self.key] = []

        self.store[self.key].append({"name": name, "doc": doc})
