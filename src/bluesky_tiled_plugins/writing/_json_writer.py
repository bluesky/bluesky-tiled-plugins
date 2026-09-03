import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# NOTE: This code is duplicated in src/bluesky/callbacks/json_writer.py


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

        if name == "start":
            with open(self.dirname / self.filename, "w") as file:
                file.write("[\n")
                json.dump({"name": name, "doc": doc}, file)
                file.write(",\n")

        elif name == "stop":
            with open(self.dirname / self.filename, "a") as file:
                json.dump({"name": name, "doc": doc}, file)
                file.write("\n]")

        else:
            with open(self.dirname / self.filename, "a") as file:
                json.dump({"name": name, "doc": doc}, file)
                file.write(",\n")


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

        with open(self.dirname / self.filename, "a") as file:
            json.dump({"name": name, "doc": doc}, file)
            file.write("\n")


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
