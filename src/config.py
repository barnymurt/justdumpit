import os
from pathlib import Path
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).parent.parent
ENV_PATH = CONFIG_DIR / ".env"

load_dotenv(ENV_PATH)


def get_data_dir() -> Path:
    """Persistent data directory. On Fly.io this is the volume mount (/data).
    Locally it stays in the project root (./data) so we don't litter the repo.
    Override with YTSCRAPER_DATA_DIR env var.
    """
    env = os.getenv("YTSCRAPER_DATA_DIR")
    if env:
        data_dir = Path(env)
    elif os.getenv("FLY_APP_NAME"):
        data_dir = Path("/data")
    else:
        data_dir = CONFIG_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_api_key() -> str:
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        raise ValueError(
            "MINIMAX_API_KEY not found. Please set it in .env file or environment variable.\n"
            "Copy .env.example to .env and add your API key."
        )
    return api_key


def get_output_dir() -> Path:
    output_dir = get_data_dir() / "output"
    output_dir.mkdir(exist_ok=True)
    return output_dir


def get_channels_file() -> Path:
    return get_data_dir() / "channels.json"


def get_backup_dir() -> Path:
    backup_dir = get_data_dir() / "backups"
    backup_dir.mkdir(exist_ok=True)
    return backup_dir


DEFAULT_MODEL = "MiniMax-M2.5"
MAX_CHUNK_SIZE = 50000
MAX_VIDEO_SELECT = 5