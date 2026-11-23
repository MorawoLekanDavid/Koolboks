from .api import create_app
from .db import Config

app = create_app(Config.from_env())
