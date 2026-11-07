"""``init_db`` upgrades a database file created before the ``category`` column existed."""

import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy import text

from app.db.database import build_engine, init_db


def test_init_db_adds_missing_category_column(tmp_path: Path) -> None:
    db_file = tmp_path / "old.db"
    with sqlite3.connect(db_file) as connection:  # the schema of the first release
        connection.execute(
            "CREATE TABLE expenses (id INTEGER PRIMARY KEY, merchant_name VARCHAR(255), date DATE, "
            "subtotal_amount NUMERIC(12,2), tax_amount NUMERIC(12,2), total_amount NUMERIC(12,2), "
            "currency VARCHAR(3) NOT NULL DEFAULT 'TRY', reconciliation_status VARCHAR(20) NOT NULL "
            "DEFAULT 'flagged_for_review', raw_text TEXT, image_path VARCHAR(512), "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT INTO expenses (merchant_name, total_amount) VALUES ('OLD SHOP', 5)")

    async def run() -> tuple[list[str], list[str], list[str | None]]:
        engine = build_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
        try:
            await init_db(engine)
            await init_db(engine)  # idempotent
            async with engine.connect() as connection:
                columns = [row[1] for row in (await connection.execute(text("PRAGMA table_info(expenses)"))).all()]
                merchants = [row[0] for row in (await connection.execute(text("SELECT merchant_name FROM expenses"))).all()]
                keys = [row[0] for row in (await connection.execute(text("SELECT merchant_key FROM expenses"))).all()]
            return columns, merchants, keys
        finally:
            await engine.dispose()

    columns, merchants, keys = asyncio.run(run())
    assert {"category", "merchant_key", "reconciliation_method"} <= set(columns)
    assert merchants == ["OLD SHOP"]  # existing data is kept
    assert keys == ["OLD SHOP"]  # and gets its merchant key
