import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
_config = None


def load_config() -> dict:
    global _config
    if _config is None:
        with open(_CONFIG_PATH) as f:
            _config = json.load(f)
    return _config
