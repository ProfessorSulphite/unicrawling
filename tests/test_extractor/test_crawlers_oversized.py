"""
The oversized-response path: narrowing an ask that does not fit.

Every ITU run of 2026-09-03 hit `RPCResponseTooLargeError` on the programme
queries -- three attempts each, all landing within 60 KB of the 50 MB ceiling,
because the retry loop re-issued an identical prompt against an identical source
set. Six minutes and three queries of daily budget bought the same wall three
times, and the payload shipped with an empty bachelors bucket.

The remedy is to shrink what the question is grounded in, not to ask it again.
"""
import json

from unittest.mock import AsyncMock, MagicMock

import pytest
from notebooklm.exceptions import RPCResponseTooLargeError
from pydantic import BaseModel
from typing import List

from src.config import config
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    QuerySpec,
    is_oversized_response_error,
    run_query,
)
from src.utilities.schema import ProgramItem


def _program(name: str) -> dict:
    return {"name": name, "degree_level": "bachelors"}


def _spec() -> QuerySpec:
    return QuerySpec(key="bachelors", prompt="list them", model=List[ProgramItem], tiers=(1, 2))


def _client(answers) -> MagicMock:
    """A client whose chat.ask returns/raises the next scripted answer."""
    client = MagicMock()
    client.chat = MagicMock()

    async def _ask(**kwargs):
        outcome = answers.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        res = MagicMock()
        res.answer = outcome
        return res

    client.chat.ask = AsyncMock(side_effect=_ask)
    return client


# ------------------------------------------------------------- recognition --

def test_the_sdk_error_is_recognised():
    assert is_oversized_response_error(
        RPCResponseTooLargeError("RPC response exceeded 52428800 bytes")
    )


def test_the_logged_message_is_recognised_even_when_wrapped():
    """The SDK wraps this in a few places; a missed match costs a degree bucket."""
    assert is_oversized_response_error(
        RuntimeError("RPC response exceeded 52428800 bytes (read 52449234 bytes before aborting)")
    )


def test_an_ordinary_error_is_not_treated_as_oversized():
    assert not is_oversized_response_error(RuntimeError("connection reset"))


# ---------------------------------------------------------------- narrowing --

async def test_an_oversized_answer_is_re_asked_over_source_halves_and_merged():
    """The whole point: 4 sources that do not fit are asked as 2 groups of 2."""
    client = _client([
        RPCResponseTooLargeError("RPC response exceeded 52428800 bytes"),
        json.dumps([_program("BS CS"), _program("BS SE")]),
        json.dumps([_program("BS EE")]),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2", "s3", "s4"], report)

    assert [p.name for p in value] == ["BS CS", "BS SE", "BS EE"]
    assert report.ok, f"a recovered query must not be reported as failed: {report.failed}"
    assert report.succeeded == ["bachelors"]
    # The halves, not the whole set, on the retries.
    scopes = [c.kwargs["source_ids"] for c in client.chat.ask.await_args_list]
    assert scopes == [["s1", "s2", "s3", "s4"], ["s1", "s2"], ["s3", "s4"]]


async def test_a_programme_found_in_both_halves_is_not_duplicated():
    """A programme whose pages straddle the split must reach the payload once."""
    client = _client([
        RPCResponseTooLargeError("too large"),
        json.dumps([_program("BS CS"), _program("BS SE")]),
        json.dumps([_program("BS SE"), _program("BS EE")]),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2"], report)

    assert [p.name for p in value] == ["BS CS", "BS SE", "BS EE"]


async def test_a_half_that_is_still_too_large_is_split_again():
    client = _client([
        RPCResponseTooLargeError("too large"),        # whole set
        RPCResponseTooLargeError("too large"),        # first half
        json.dumps([_program("BS CS")]),              # first quarter
        json.dumps([_program("BS SE")]),              # second quarter
        json.dumps([_program("BS EE")]),              # second half
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2", "s3", "s4"], report)

    assert [p.name for p in value] == ["BS CS", "BS SE", "BS EE"]


async def test_splitting_stops_at_the_configured_depth(monkeypatch):
    """Bounded: an answer that never fits fails rather than fanning out forever."""
    monkeypatch.setattr(config, "max_query_split_depth", 1)
    client = _client([RPCResponseTooLargeError("too large")] * 20)
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2", "s3", "s4"], report)

    assert value is None
    assert "bachelors" in report.failed
    # One whole-set ask plus one level of halves, and nothing beyond.
    assert client.chat.ask.await_count == 3


async def test_an_oversized_query_does_not_burn_the_repair_retries():
    """
    The old loop spent max_query_retries + 1 identical asks on a deterministic
    failure. Narrowing must replace those attempts, not follow them.
    """
    client = _client([RPCResponseTooLargeError("too large")] * 20)
    report = ExtractionReport()

    await run_query(client, "nb-1", _spec(), ["s1"], report)

    # A single source cannot be narrowed, so exactly one ask is issued -- not the
    # three the repair loop used to spend.
    assert client.chat.ask.await_count == 1


async def test_a_malformed_answer_still_gets_its_repair_retry():
    """Narrowing must not have cost the repair path its reason for existing."""
    client = _client([
        "here is the JSON you asked for: [{\"name\": \"BS CS\", \"degree_level\": ",
        json.dumps([_program("BS CS")]),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1"], report)

    assert [p.name for p in value] == ["BS CS"]
    assert client.chat.ask.await_count == 2


# ------------------------------------------------------------------ ledger --

async def test_a_parse_failure_is_charged_once_not_twice():
    """
    queries_used was incremented inside the try AND again in the generic except,
    so a validation error that is not an ExtractionError double-charged the
    daily ledger.
    """
    client = _client([
        json.dumps([_program("BS CS")]),
    ])
    report = ExtractionReport()

    await run_query(client, "nb-1", _spec(), ["s1"], report)

    assert report.queries_used == 1


async def test_every_narrowed_sub_ask_is_charged_to_the_budget():
    client = _client([
        RPCResponseTooLargeError("too large"),
        json.dumps([_program("BS CS")]),
        json.dumps([_program("BS EE")]),
    ])
    report = ExtractionReport()

    await run_query(client, "nb-1", _spec(), ["s1", "s2"], report)

    assert report.queries_used == 3, "a narrowed ask still spends a real query"


async def test_a_single_object_query_takes_the_first_answer_rather_than_merging():
    """main_info/contact cannot be concatenated: both halves describe one university."""
    class _Obj(BaseModel):
        name: str

    spec = QuerySpec(key="main_info_contact", prompt="p", model=_Obj, tiers=(1,))
    client = _client([
        RPCResponseTooLargeError("too large"),
        json.dumps({"name": "ITU"}),
        json.dumps({"name": "ITU"}),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", spec, ["s1", "s2"], report)

    assert value.name == "ITU"
    assert report.ok
