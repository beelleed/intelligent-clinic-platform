"""Local review database for proposed Taiwanese clinic content.

This module is intentionally not wired to public API routes or the agent. A
clinic must review the entries and supply its actual operational data first.
"""

import argparse
import json
import sqlite3
from pathlib import Path


SEED_PATH = Path(__file__).resolve().parents[1] / "clinic_content" / "seed.json"


def initialize_review_database(database_path: Path, seed_path: Path = SEED_PATH) -> None:
    """Create a local SQLite review database without replacing existing edits."""
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS clinic_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                locale TEXT NOT NULL,
                clinic_type TEXT NOT NULL,
                review_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS faqs (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                requires_live_data INTEGER NOT NULL CHECK (requires_live_data IN (0, 1)),
                review_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS medications (
                ingredient TEXT PRIMARY KEY,
                general_use TEXT NOT NULL,
                possible_effects TEXT NOT NULL,
                source_url TEXT NOT NULL,
                review_status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO clinic_profile VALUES (1, ?, ?, ?)",
            (data["locale"], data["clinic_type"], data["review_status"]),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO faqs
               (id, category, question, answer, requires_live_data, review_status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                (item["id"], item["category"], item["question"], item["answer"],
                 int(item["requires_live_data"]), item["review_status"])
                for item in data["faqs"]
            ),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO medications
               (ingredient, general_use, possible_effects, source_url, review_status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                (item["ingredient"], item["general_use"], item["possible_effects"],
                 item["source_url"], item["review_status"])
                for item in data["medications"]
            ),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the local clinic content review database")
    parser.add_argument("--db", type=Path, default=Path("data/clinic_content_review_en.db"))
    args = parser.parse_args()
    initialize_review_database(args.db)
    print(f"Review database ready: {args.db}")
