import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    brand_name: str = os.getenv("LA_BRAND_NAME", "Northwind Finance")
    assistant_name: str = os.getenv("LA_ASSISTANT_NAME", "Aria")
    mock_base_url: str = os.getenv("LA_MOCK_BASE_URL", "http://127.0.0.1:8100")
    data_dir: Path = Path(os.getenv("LA_DATA_DIR", ROOT / "data"))
    config_dir: Path = ROOT / "config"
    max_tool_rounds: int = int(os.getenv("LA_MAX_TOOL_ROUNDS", "8"))
    max_output_tokens: int = int(os.getenv("LA_MAX_OUTPUT_TOKENS", "16000"))
    # Hard ceiling on one tool call, retries included, so a slow system can't stall a conversation.
    tool_timeout_s: float = float(os.getenv("LA_TOOL_TIMEOUT_S", "8"))
    # Demo mode shows OTPs in the UI instead of sending an SMS.
    demo_mode: bool = os.getenv("LA_DEMO_MODE", "1") == "1"
    session_ttl_s: int = int(os.getenv("LA_SESSION_TTL_S", "3600"))
    systems_config: Path = field(default_factory=lambda: ROOT / "config" / "systems.yaml")


settings = Settings()
