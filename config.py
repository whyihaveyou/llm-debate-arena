import os
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).parent
CONFIG_PATH = CONFIG_DIR / "config.json"
CONVERSATIONS_DIR = CONFIG_DIR / "conversations"

DEFAULT_MODELS = {
    "mimo-v2-pro": {
        "name": "小米 MiMo V2 Pro",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key": "",
        "model": "mimo-v2-pro",
    },
    "kimi-k2.5": {
        "name": "Kimi K2.5",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "",
        "model": "kimi-k2.5",
    },
}


def load_config() -> dict:
    config = {
        "models": {k: dict(v) for k, v in DEFAULT_MODELS.items()},
        "server": {"host": "0.0.0.0", "port": 8765},
        "password": os.environ.get("DEBATE_PASSWORD", "debate2026"),
        "max_rounds": 20,
        "disagreement_threshold": 5,
        "quota": {"monthly_limit": 50, "topic_max_length": 2000},
        "rate_limit": {"max_debates_per_hour": 10, "max_concurrent_debates": 3},
        "cors": {"allowed_origins": ["*"]},
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_config = json.load(f)
        for key in ("models", "server", "password", "max_rounds", "disagreement_threshold",
                     "quota", "rate_limit", "cors"):
            if key in user_config:
                config[key] = user_config[key]
    for model_id in list(config["models"].keys()):
        env_key = model_id.upper().replace("-", "_") + "_API_KEY"
        if os.environ.get(env_key):
            config["models"][model_id]["api_key"] = os.environ[env_key]
    return config
