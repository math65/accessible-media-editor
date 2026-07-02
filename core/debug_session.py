import json
import os

APP_DIR_NAME = "AccessibleMediaEditor"


def get_appdata_dir():
    appdata = os.getenv("APPDATA")
    if appdata:
        return appdata
    return os.path.expanduser("~")


def get_config_dir():
    return os.path.join(get_appdata_dir(), APP_DIR_NAME)


def ensure_config_dir():
    path = get_config_dir()
    os.makedirs(path, exist_ok=True)
    return path


def get_config_path():
    return os.path.join(get_config_dir(), "config.json")


def load_raw_config():
    config_path = get_config_path()
    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def save_raw_config(config_data):
    ensure_config_dir()
    with open(get_config_path(), "w", encoding="utf-8") as handle:
        json.dump(config_data, handle, indent=4)
