import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from src.utilities.state_management import StateManager
from src.utilities.schema import ProgramItem, DegreeLevel, NormalizedTuition, UniversityPayload, MainInfo, KeyLinks, ContactInfo, ProgramCategoryBlock


@pytest.fixture
def temp_state_db(tmp_path: Path):
    db_file = tmp_path / "test_state.sqlite"
    mgr = StateManager(db_path=db_file)
    yield mgr
    mgr.close()


def test_state_schema_ttl_columns_migrated(temp_state_db: StateManager):
    """Verify that all TTL, versioning, and intake columns exist and auto-migrate."""
    with temp_state_db._get_connection() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_state);").fetchall()}
        assert "intake_year" in cols
        assert "data_version" in cols
        assert "ttl_days" in cols
        assert "expires_at" in cols


def test_set_status_computes_expires_at_on_completion(temp_state_db: StateManager):
    """Setting completed status calculates an expires_at timestamp based on ttl_days."""
    temp_state_db.set_status("test-uni", "crawled")
    state = temp_state_db.get_state("test-uni")
    assert state["expires_at"] is None

    # Transition to completed with 30 days TTL
    temp_state_db.set_status("test-uni", "completed", ttl_days=30, intake_year="2026")
    state = temp_state_db.get_state("test-uni")
    assert state["status"] == "completed"
    assert state["intake_year"] == "2026"
    assert state["ttl_days"] == 30
    assert state["expires_at"] is not None

    exp = datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    # Check that expiration is ~30 days in the future
    diff_days = (exp.replace(tzinfo=timezone.utc) - now).days
    assert 28 <= diff_days <= 31


def test_get_completed_slugs_filters_expired_and_intake_year(temp_state_db: StateManager):
    """get_completed_slugs skips expired records and older intake years."""
    # 1. Valid record for 2026
    temp_state_db.set_status("uni-valid", "completed", intake_year="2026", ttl_days=180)

    # 2. Record from older intake year 2025
    temp_state_db.set_status("uni-2025", "completed", intake_year="2025", ttl_days=180)

    # 3. Expired record
    temp_state_db.set_status("uni-expired", "completed", intake_year="2026", ttl_days=-5)

    # By default, only unexpired 2026 completed records are returned
    completed = temp_state_db.get_completed_slugs(current_intake_year="2026")
    assert "uni-valid" in completed
    assert "uni-2025" not in completed
    assert "uni-expired" not in completed

    # Include expired should return all 3
    all_completed = temp_state_db.get_completed_slugs(include_expired=True)
    assert set(all_completed) == {"uni-valid", "uni-2025", "uni-expired"}


def test_expire_stale_records(temp_state_db: StateManager):
    """expire_stale_records transitions expired records to pending."""
    temp_state_db.set_status("uni-fresh", "completed", ttl_days=100)
    temp_state_db.set_status("uni-stale", "completed", ttl_days=-1)

    expired_count = temp_state_db.expire_stale_records()
    assert expired_count == 1

    assert temp_state_db.get_status("uni-fresh") == "completed"
    assert temp_state_db.get_status("uni-stale") == "pending"


def test_normalized_tuition_schema():
    """Verify NormalizedTuition model serialization in ProgramItem."""
    prog = ProgramItem(
        name="BS Computer Science",
        degree_level=DegreeLevel.BACHELORS,
        tuition_fee="$15,000 / year",
        tuition_fee_normalized=NormalizedTuition(
            amount=15000.0,
            currency="USD",
            interval="annual",
            normalized_usd=15000.0,
            raw_fee="$15,000 / year",
        ),
    )
    assert prog.tuition_fee_normalized is not None
    assert prog.tuition_fee_normalized.amount == 15000.0
    assert prog.tuition_fee_normalized.currency == "USD"
    assert prog.tuition_fee_normalized.normalized_usd == 15000.0

    # Payload with intake_year and data_version
    payload = UniversityPayload(
        main_info=MainInfo(
            name="Test University",
            website="https://test.edu",
            description="A test university",
            key_links=KeyLinks(application_portal_url="https://apply.test.edu"),
        ),
        programs=ProgramCategoryBlock(bachelors=[prog]),
        contact=ContactInfo(),
        intake_year="2026",
        data_version=1,
    )
    assert payload.intake_year == "2026"
    assert payload.data_version == 1
