import json

import uuid
from pathlib import Path
import jinja2


def render_templated_documents(fname: str, data_dir: str):
    dirpath = str(Path(__file__).parent)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(dirpath), autoescape=False)
    template = env.get_template(fname)
    rendered = template.render(root_path=data_dir, uuid=str(uuid.uuid4())[:-12])

    yield from json.loads(rendered)
