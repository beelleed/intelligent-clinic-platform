import json
import sqlite3
from pathlib import Path

from app.clinic_content import SEED_PATH, initialize_review_database


def test_seed_is_review_only_and_contains_no_dosing() -> None:
    content = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    assert content["locale"] == "en"
    assert content["review_status"] == "pending_clinic_review"
    assert len(content["faqs"]) >= 10
    assert len(content["medications"]) == 4
    assert all(item["review_status"].startswith("pending_") for item in content["faqs"])
    assert all(item["review_status"] == "pending_pharmacist_review" for item in content["medications"])
    assert all(item["source_url"].startswith("https://www.fda.gov.tw/") for item in content["medications"])
    assert all("dose" not in item and "dosage" not in item for item in content["medications"])


def test_database_keeps_review_edits_when_initialized_again(tmp_path: Path) -> None:
    database_path = tmp_path / "clinic_content.db"
    initialize_review_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM faqs").fetchone()[0] == 11
        assert connection.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 4
        assert connection.execute(
            "SELECT requires_live_data FROM faqs WHERE id = 'current-number'"
        ).fetchone()[0] == 1
        connection.execute("UPDATE faqs SET answer = 'Clinic reviewed' WHERE id = 'hours'")
    initialize_review_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT answer FROM faqs WHERE id = 'hours'"
        ).fetchone()[0] == "Clinic reviewed"
