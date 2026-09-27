"""Tests for the price table and cost arithmetic (nimbus.pricing).

The one rule that matters most: a model with no known price costs ``None``
("unknown"), never ``0.0``. A zero would read as free and let a budget believe
nothing had been spent.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from nimbus import pricing
from nimbus.pricing import Price

MILLION = 1_000_000


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.delenv(pricing.PRICING_FILE_ENV, raising=False)
    pricing.reset_cache()
    yield
    pricing.reset_cache()


@pytest.fixture
def override(tmp_path, monkeypatch):
    """Write ``models`` as the override file and point the env var at it."""
    path = tmp_path / "prices.json"

    def write(models, *, raw: str | None = None) -> None:
        path.write_text(raw if raw is not None else json.dumps({"models": models}))
        # A fresh mtime, so a rewrite within the same second is still noticed.
        stamp = time.time() + len(path.read_text())
        os.utime(path, (stamp, stamp))
        monkeypatch.setenv(pricing.PRICING_FILE_ENV, str(path))

    return write


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
    }


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("bedrock", "amazon.nova-pro-v1:0", "amazon.nova-pro"),
        ("bedrock", "us.amazon.nova-pro-v1:0", "amazon.nova-pro"),
        ("bedrock", "meta.llama3-1-70b-instruct-v1:0", "meta.llama3-1-70b-instruct"),
        ("bedrock", "mistral.mistral-7b-instruct-v0:2", "mistral.mistral-7b-instruct"),
        ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet"),
        ("bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        ("bedrock", "global.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("bedrock", "anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
        (
            "bedrock",
            "arn:aws:bedrock:us-east-1:123:inference-profile/us.meta.llama3-3-70b-instruct-v1:0",
            "meta.llama3-3-70b-instruct",
        ),
        ("bedrock", "openai.gpt-oss-120b-1:0", "openai.gpt-oss-120b-1"),
        ("anthropic", "claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("anthropic", "claude-opus-4-1", "claude-opus-4-1"),
        ("openai", "gpt-5-mini-2025-08-07", "gpt-5-mini"),
        ("openai", "gpt-5.2", "gpt-5.2"),
        ("ollama", " llama3.2:latest ", "llama3.2:latest"),
    ],
)
def test_model_ids_normalize_to_their_table_key(provider, model_id, expected):
    assert pricing.normalize_model_id(provider, model_id) == expected


def test_a_region_prefix_is_not_mistaken_for_a_vendor():
    """``me.`` is a region prefix; ``meta.`` is a vendor. Only the former is dropped."""
    assert pricing.normalize_model_id("bedrock", "meta.llama3-8b-instruct-v1:0") == (
        "meta.llama3-8b-instruct"
    )


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def test_a_bedrock_model_costs_its_list_price():
    cost = pricing.cost_usd("bedrock", "amazon.nova-pro-v1:0", usage(MILLION, MILLION))
    assert cost == pytest.approx(0.8 + 3.2)


def test_a_cross_region_profile_costs_the_same_as_the_model_when_there_is_no_premium():
    plain = pricing.lookup("bedrock", "amazon.nova-lite-v1:0")
    assert pricing.lookup("bedrock", "us.amazon.nova-lite-v1:0") == plain


def test_bedrock_claude_is_cheaper_on_the_global_endpoint():
    """Claude 4.5+ on Bedrock: regional and geo profiles pay 10% over ``global.``."""
    global_price = pricing.lookup("bedrock", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
    regional = pricing.lookup("bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    in_region = pricing.lookup("bedrock", "anthropic.claude-sonnet-4-5-20250929-v1:0")

    assert (global_price.input, global_price.output) == (3.0, 15.0)
    assert regional.input == pytest.approx(3.3)
    assert regional.output == pytest.approx(16.5)
    assert regional.cache_read == pytest.approx(0.33)
    assert regional.cache_write == pytest.approx(4.125)
    assert in_region == regional


def test_older_claude_models_have_no_regional_premium():
    assert pricing.lookup("bedrock", "us.anthropic.claude-3-5-haiku-20241022-v1:0") == Price(
        0.8, 4.0, 0.08, 1.0
    )


def test_the_anthropic_api_prices_dated_and_undated_ids_alike():
    dated = pricing.lookup("anthropic", "claude-haiku-4-5-20251001")
    alias = pricing.lookup("anthropic", "claude-haiku-4-5")
    assert dated == alias
    assert (dated.input, dated.output) == (1.0, 5.0)


def test_the_anthropic_api_has_no_regional_premium():
    assert pricing.lookup("anthropic", "claude-sonnet-4-5").input == 3.0


def test_openai_counts_cached_tokens_inside_input_tokens():
    """OpenAI's ``prompt_tokens`` includes the cached ones: each is priced once."""
    cost = pricing.cost_usd("openai", "gpt-5-2025-08-07", usage(MILLION, 0, cache_read=MILLION))
    assert cost == pytest.approx(0.125)


def test_bedrock_and_anthropic_count_cached_tokens_separately():
    cost = pricing.cost_usd(
        "anthropic",
        "claude-sonnet-4-5",
        usage(MILLION, MILLION, cache_read=MILLION, cache_write=MILLION),
    )
    assert cost == pytest.approx(3.0 + 15.0 + 0.3 + 3.75)


def test_cached_tokens_fall_back_to_the_input_price_when_none_is_published():
    """Never free: a model with no cache price pays its input rate for cache traffic."""
    cost = pricing.cost_usd(
        "bedrock", "anthropic.claude-3-haiku-20240307-v1:0", usage(cache_read=MILLION)
    )
    assert cost == pytest.approx(0.25)


def test_ollama_is_free():
    assert pricing.cost_usd("ollama", "llama3.2", usage(MILLION, MILLION)) == 0.0


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("bedrock", "amazon.titan-text-express-v1"),
        ("bedrock", "fake.model"),
        ("anthropic", "claude-2.1"),
        ("openai", "gpt-4o"),
        ("somewhere-else", "anything"),
    ],
)
def test_an_unknown_model_costs_none_never_zero(provider, model_id):
    assert pricing.lookup(provider, model_id) is None
    assert not pricing.is_priced(provider, model_id)
    assert pricing.cost_usd(provider, model_id, usage(MILLION, MILLION)) is None


def test_no_usage_costs_nothing_on_a_priced_model():
    assert pricing.cost_usd("bedrock", "amazon.nova-micro-v1:0", None) == 0.0


def test_costs_are_rounded_to_a_millionth_of_a_dollar():
    cost = pricing.cost_usd("bedrock", "amazon.nova-micro-v1:0", usage(11, 7))
    assert cost == round((11 * 0.035 + 7 * 0.14) / MILLION, 6)


def test_every_built_in_price_is_sane():
    tables = [pricing.ANTHROPIC_PRICES, pricing.BEDROCK_PRICES, pricing.OPENAI_PRICES]
    for table in tables:
        for key, price in table.items():
            assert price.input >= 0 and price.output >= price.input * 0.5, key
            assert price.regional_multiplier in (1.0, 1.1), key


# --------------------------------------------------------------------------- #
# Override file
# --------------------------------------------------------------------------- #


def test_an_override_prices_an_unknown_model(override):
    override({"fake.model": {"input": 2, "output": 4}})
    assert pricing.cost_usd("bedrock", "fake.model", usage(MILLION, MILLION)) == 6.0


def test_an_override_can_be_scoped_to_a_provider(override):
    override({"openai:gpt-4o": {"input": 2.5, "output": 10, "cache_read": 1.25}})
    assert pricing.lookup("openai", "gpt-4o-2024-08-06") == Price(2.5, 10.0, 1.25, None)
    assert pricing.lookup("bedrock", "gpt-4o") is None


def test_an_override_wins_over_the_built_in_price(override):
    override({"bedrock:amazon.nova-pro-v1:0": {"input": 0.1, "output": 0.2}})
    assert pricing.lookup("bedrock", "amazon.nova-pro-v1:0") == Price(0.1, 0.2)


def test_an_override_of_null_makes_a_model_unknown(override):
    override({"anthropic:claude-opus-4-1": None})
    assert pricing.cost_usd("anthropic", "claude-opus-4-1-20250805", usage(1, 1)) is None


def test_models_not_in_the_override_keep_their_built_in_price(override):
    override({"fake.model": {"input": 1, "output": 1}})
    assert pricing.lookup("bedrock", "amazon.nova-pro-v1:0").input == 0.8


def test_an_edited_override_file_is_re_read(override):
    override({"fake.model": {"input": 1, "output": 1}})
    assert pricing.lookup("bedrock", "fake.model").input == 1.0
    override({"fake.model": {"input": 5, "output": 5}})
    assert pricing.lookup("bedrock", "fake.model").input == 5.0


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"models": []}),
        json.dumps({"models": {"x": "cheap"}}),
        json.dumps({"models": {"x": {"input": 1}}}),
        json.dumps({"models": {"x": {"input": -1, "output": 1}}}),
        json.dumps({"models": {"x": {"input": True, "output": 1}}}),
    ],
)
def test_a_broken_override_file_is_ignored_with_a_warning(override, raw, caplog):
    override(None, raw=raw)
    assert pricing.lookup("bedrock", "amazon.nova-pro-v1:0").input == 0.8
    assert pricing.lookup("bedrock", "x") is None
    assert "pricing file" in caplog.text


def test_a_missing_override_file_is_ignored_with_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(pricing.PRICING_FILE_ENV, str(tmp_path / "nope.json"))
    assert pricing.lookup("bedrock", "amazon.nova-pro-v1:0").input == 0.8
    assert "unreadable" in caplog.text
