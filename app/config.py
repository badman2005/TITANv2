"""Environment-driven settings (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://titan:titan_pass@127.0.0.1:3306/titan_db?charset=utf8mb4",
    )
    auto_create_tables: bool = os.getenv("TITAN_AUTO_CREATE", "0") == "1"

    temp_min_c: float = _f("TEMP_MIN_C", 2.0)
    temp_max_c: float = _f("TEMP_MAX_C", 8.0)
    mkt_window_hours: float = _f("MKT_WINDOW_HOURS", 24)
    expiring_soon_days: int = int(os.getenv("EXPIRING_SOON_DAYS", "14"))
    max_future_skew_s: int = int(os.getenv("MAX_FUTURE_SKEW_S", "300"))

    cors_origins: tuple = tuple(
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    )

    demo_enabled: bool = os.getenv("TITAN_DEMO", "1") == "1"
    self_url: str = os.getenv("TITAN_SELF_URL", f"http://127.0.0.1:{os.getenv('PORT', '8000')}")

    anchor_backend: str = os.getenv("ANCHOR_BACKEND", "mock").lower()
    ipfs_api_url: str = os.getenv("IPFS_API_URL", "")
    ipfs_api_auth: str = os.getenv("IPFS_API_AUTH", "")
    polygon_rpc_url: str = os.getenv("POLYGON_RPC_URL", "")
    polygon_chain_id: int = int(os.getenv("POLYGON_CHAIN_ID", "80002"))
    polygon_private_key: str = os.getenv("POLYGON_PRIVATE_KEY", "")


settings = Settings()
