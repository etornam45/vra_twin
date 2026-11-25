"""Local development entry point for the central API and dashboard."""

import os
from pathlib import Path

import uvicorn

from backend.src.api.app import create_backend_app


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    db_path = None if database_url else Path(os.environ.get("VRA_DB_PATH", "vra-twin.db"))
    host = os.environ.get("VRA_API_HOST", "127.0.0.1")
    port = int(os.environ.get("VRA_API_PORT", "8000"))
    uvicorn.run(create_backend_app(db_path, database_url), host=host, port=port)


if __name__ == "__main__":
    main()
