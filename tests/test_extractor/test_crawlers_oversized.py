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
    _attempt_query,
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

async def test_an_oversized_answer_is_re_asked_over_source_halves_and_merged(oversize_split_enabled):
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


async def test_a_programme_found_in_both_halves_is_not_duplicated(oversize_split_enabled):
    """A programme whose pages straddle the split must reach the payload once."""
    client = _client([
        RPCResponseTooLargeError("too large"),
        json.dumps([_program("BS CS"), _program("BS SE")]),
        json.dumps([_program("BS SE"), _program("BS EE")]),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2"], report)

    assert [p.name for p in value] == ["BS CS", "BS SE", "BS EE"]


async def test_a_half_that_is_still_too_large_is_split_again(oversize_split_enabled):
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


async def test_splitting_stops_at_the_configured_depth(monkeypatch, oversize_split_enabled):
    """Bounded: an answer that never fits fails rather than fanning out forever."""
    monkeypatch.setattr(config, "max_query_split_depth", 1)
    client = _client([RPCResponseTooLargeError("too large")] * 20)
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2", "s3", "s4"], report)

    assert value is None
    assert "bachelors" in report.failed
    # One whole-set ask plus one level of halves, and nothing beyond.
    assert client.chat.ask.await_count == 3


async def test_an_oversized_query_does_not_burn_the_repair_retries(oversize_split_enabled):
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


async def test_every_narrowed_sub_ask_is_charged_to_the_budget(oversize_split_enabled):
    client = _client([
        RPCResponseTooLargeError("too large"),
        json.dumps([_program("BS CS")]),
        json.dumps([_program("BS EE")]),
    ])
    report = ExtractionReport()

    await run_query(client, "nb-1", _spec(), ["s1", "s2"], report)

    assert report.queries_used == 3, "a narrowed ask still spends a real query"


async def test_a_single_object_query_takes_the_first_answer_rather_than_merging(oversize_split_enabled):
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


# ------------------------------------------ the repair prompt (C32) ---------

def _recording_client(answers):
    """Like _client, but keeps every prompt it was sent."""
    client = _client(answers)
    client.prompts = []
    original = client.chat.ask.side_effect

    async def _ask(**kwargs):
        client.prompts.append(kwargs.get("question", ""))
        return await original(**kwargs)

    client.chat.ask = AsyncMock(side_effect=_ask)
    return client


async def test_a_repair_re_ask_quotes_the_answer_that_failed():
    """
    The repair loop's whole mechanism is showing the model its own broken output.

    `_attempt_query` assigned `raw = ""` at the top of each attempt and then
    interpolated `raw[:1500]` into the repair prompt three lines below, so every
    re-ask said "Previous answer (truncated):" followed by nothing -- the
    feature was disabled by its own guard. Yale burned attempts 1 and 3 on the
    byte-identical error on 2026-09-16 because of this.
    """
    client = _recording_client(["{ this is not json", json.dumps([_program("BS CS")])])
    report = ExtractionReport()

    value, error = await _attempt_query(client, "nb-1", _spec(), ["s1"], report)

    assert error is None and [p.name for p in value] == ["BS CS"]
    assert len(client.prompts) == 2
    repair = client.prompts[1]
    assert "this is not json" in repair, "the repair prompt did not quote the failed answer"
    assert "Previous answer (truncated):\n\n" not in repair


async def test_a_transport_failure_quotes_nothing_rather_than_a_stale_answer():
    """
    The guard that broke the feature was protecting something real: after a
    transport failure there IS no answer, and quoting an earlier attempt's would
    tell the model to repair text it never sent. Both halves now hold.
    """
    client = _recording_client([
        RuntimeError("connection reset"),
        json.dumps([_program("BS CS")]),
    ])
    report = ExtractionReport()

    value, error = await _attempt_query(client, "nb-1", _spec(), ["s1"], report)

    assert error is None and value
    assert "Your previous answer did not arrive." in client.prompts[1]


async def test_an_oversized_response_is_re_asked_once_identically(monkeypatch):
    """
    The evidence says this failure is transient, not a property of the corpus:
    `phd` and `diploma` succeeded over the SAME 37 sources that `bachelors` and
    `masters` failed on twice each. So the cheap remedy is tried first.
    """
    monkeypatch.setattr(config, "enable_oversize_split", False)
    monkeypatch.setattr(config, "oversize_single_reask", True)

    client = _client([
        RPCResponseTooLargeError("RPC response exceeded 52428800 bytes"),
        json.dumps([_program("BS CS")]),
    ])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2"], report)

    assert [p.name for p in value] == ["BS CS"]
    assert report.ok
    assert report.queries_used == 2


async def test_source_splitting_is_off_by_default(monkeypatch):
    """
    Splitting spent three asks on ITU and returned three byte-identical
    sub-answers -- no new evidence for a quarter of the university's quota. It
    stays reachable for one release but must not run unasked.
    """
    monkeypatch.setattr(config, "oversize_single_reask", False)

    client = _client([RPCResponseTooLargeError("RPC response exceeded 52428800 bytes")])
    report = ExtractionReport()

    value = await run_query(client, "nb-1", _spec(), ["s1", "s2", "s3", "s4"], report)

    assert value is None
    assert report.queries_used == 1, "the split path ran without being enabled"
    assert "bachelors" in report.failed
