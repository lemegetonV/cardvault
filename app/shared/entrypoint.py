"""Container entrypoint: run migrations for ``$SERVICE``, then serve it with uvicorn.

The Python services and mocks share one image recipe; ``SERVICE`` picks the module:
``services.<name>.main:app`` or ``mocks.<name>.main:app``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import uvicorn

from shared import migrate


def main() -> None:
    service = os.environ["SERVICE"]
    package = f"services.{service}"
    if importlib.util.find_spec(package) is None:
        package = f"mocks.{service}"
    root = Path(importlib.util.find_spec(package).origin).parent  # type: ignore[union-attr]
    migrations = root / "migrations"
    database_url = os.environ.get("DATABASE_URL")
    if migrations.is_dir() and database_url:
        migrate.upgrade(migrations, database_url)
    uvicorn.run(
        f"{package}.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
