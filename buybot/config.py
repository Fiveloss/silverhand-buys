"""Settings from the environment, optionally loaded from a .env file next to the code."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path) -> None:
    """KEY=VALUE lines; # comments; existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _ids(value: str) -> frozenset[int]:
    return frozenset(int(x) for x in value.replace(" ", "").split(",") if x)


def _plans(value: str) -> dict[int, int]:
    """"1:500,4:1500" -> {1: 500, 4: 1500} (weeks: stars)."""
    out = {}
    for part in value.replace(" ", "").split(","):
        if part:
            weeks, stars = part.split(":")
            out[int(weeks)] = int(stars)
    return out


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    db_path: str = "data/bot.db"
    poll_seconds: float = 3.0
    rpc_min_interval: float = 0.3
    explorer_url: str = "https://robinhoodchain.blockscout.com"
    trade_url: str = "https://gmgn.ai/robinhood/token/{token}"
    pro_plans: dict[int, int] = field(default_factory=lambda: {1: 500, 4: 1500})
    ad_day_stars: int = 750
    ad_max_days: int = 7
    support_contact: str = ""
    source_url: str = ""
    log_level: str = "INFO"


def load_config(env_file: str | Path = ".env") -> Config:
    load_dotenv(env_file)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    e = os.environ.get
    return Config(
        bot_token=token,
        owner_ids=_ids(e("OWNER_IDS", "")),
        rpc_url=e("RPC_URL", Config.rpc_url),
        db_path=e("DB_PATH", Config.db_path),
        poll_seconds=float(e("POLL_SECONDS", Config.poll_seconds)),
        rpc_min_interval=float(e("RPC_MIN_INTERVAL", Config.rpc_min_interval)),
        explorer_url=e("EXPLORER_URL", Config.explorer_url).rstrip("/"),
        trade_url=e("TRADE_URL", Config.trade_url),
        pro_plans=_plans(e("PRO_PLANS", "1:500,4:1500")),
        ad_day_stars=int(e("AD_DAY_STARS", Config.ad_day_stars)),
        ad_max_days=int(e("AD_MAX_DAYS", Config.ad_max_days)),
        support_contact=e("SUPPORT_CONTACT", ""),
        source_url=e("SOURCE_URL", ""),
        log_level=e("LOG_LEVEL", "INFO"),
    )
