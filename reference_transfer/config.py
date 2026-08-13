from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Small dotenv reader so the runtime has no secret-management dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    todoist_token: str
    workflowy_key: str
    database_path: Path
    todoist_rpm: int = 20
    workflowy_rpm: int = 20
    workflowy_root_name: str = "Todoist References"
    lease_seconds: int = 900
    batch_size: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        missing = [name for name in ("TODOIST_API_TOKEN", "WORKFLOWY_API_KEY") if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
        return cls(
            todoist_token=os.environ["TODOIST_API_TOKEN"],
            workflowy_key=os.environ["WORKFLOWY_API_KEY"],
            database_path=Path(os.getenv("DATABASE_PATH", "data/reference-transfer.sqlite3")),
            todoist_rpm=int(os.getenv("TODOIST_REQUESTS_PER_MINUTE", "20")),
            workflowy_rpm=int(os.getenv("WORKFLOWY_REQUESTS_PER_MINUTE", "20")),
            workflowy_root_name=os.getenv("WORKFLOWY_ROOT_NAME", "Todoist References"),
            lease_seconds=int(os.getenv("LEASE_SECONDS", "900")),
        )
