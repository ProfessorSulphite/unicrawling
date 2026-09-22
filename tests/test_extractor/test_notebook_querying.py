"""
Tests for notebook_querying text protocol integration and circuit breaker.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.config import config
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    QuotaThrottledError,
    QuerySpec,
    is_empty_stream_throttle_error,
    parse_answer,
    run_query,
    QUERY_SUITE,
)
from src.extractor.crawlers.text_protocol import PROGRAM_FIELDS, IDENTITY_FIELDS
from src.utilities.schema import ProgramItem, MainInfo, ContactInfo, DegreeLevel


def test_is_empty_stream_throttle_error():
    err1 = RuntimeError("No parseable chunks in streaming chat response (4 lines scanned). The response was empty or the API wire format may have changed.")
    assert is_empty_stream_throttle_error(err1) is True

    err2 = ValueError("4 lines scanned")
    assert is_empty_stream_throttle_error(err2) is True

    err3 = Exception("Regular network timeout")
    assert is_empty_stream_throttle_error(err3) is False


def test_parse_answer_delimited_text():
    raw_text = """
@@RECORD
NAME: BS Computer Science
LEVEL: bachelors
DURATION: 4 Years
TUITION: $25,000
CURRENCY: USD
DESCRIPTION: Complete undergraduate computer science curriculum.
@@END
"""
    spec = [s for s in QUERY_SUITE if s.key == "bachelors"][0]
    report = ExtractionReport()
    items = parse_answer(raw_text, spec, report)
    assert len(items) == 1
    assert isinstance(items[0], ProgramItem)
    assert items[0].name == "BS Computer Science"
    assert items[0].degree_level == DegreeLevel.BACHELORS
    assert items[0].tuition_fee == "$25,000"
    assert items[0].currency == "USD"


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_consecutive_throttle_errors():
    client = MagicMock()
    throttle_err = RuntimeError("No parseable chunks in streaming chat response (4 lines scanned). The response was empty or the API wire format may have changed.")
    client.chat.ask = AsyncMock(side_effect=throttle_err)

    report = ExtractionReport()
    spec1 = [s for s in QUERY_SUITE if s.key == "bachelors"][0]
    spec2 = [s for s in QUERY_SUITE if s.key == "masters"][0]

    # First error records failure and increments consecutive count
    res1 = await run_query(client, "nb-test", spec1, None, report)
    assert res1 is None
    assert report.consecutive_throttle_errors == 1

    # Second error trips circuit breaker and raises QuotaThrottledError
    with pytest.raises(QuotaThrottledError):
        await run_query(client, "nb-test", spec2, None, report)
