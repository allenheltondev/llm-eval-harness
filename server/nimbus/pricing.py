"""What a run costs: a per-model price table and the arithmetic on top of it.

Every figure here is an **estimate**. It is the published on-demand list price
per million tokens, multiplied by the token counts the provider reported. It
does not know about negotiated discounts, free tiers, batch pricing,
provisioned throughput, long-context surcharges, taxes or anything else on a
real invoice. Use it to compare runs and to bound an evaluation's spend, and
use the provider's bill for accounting.

Unknown means unknown
---------------------
A model that is not in the table (and not in an override file) has **no**
price: :func:`cost_usd` returns ``None``, never ``0.0``. A zero would read as
"free" and would let a budget believe nothing had been spent. Ollama is the
one provider priced at zero, because it runs on your own hardware.

Where the numbers come from (as of :data:`PRICING_AS_OF`)
---------------------------------------------------------
* **Bedrock**: the AWS Price List API, region ``us-east-1``: the
  ``AmazonBedrock`` offer (Amazon Nova, Meta, Mistral, DeepSeek, OpenAI
  open-weight models) and the ``AmazonBedrockFoundationModels`` offer
  (Anthropic, Cohere, AI21), both published 2026-09-25/26. Other regions can
  differ.
* **Anthropic**: https://platform.claude.com/docs/en/about-claude/pricing
  (base input, 5-minute cache write, cache hit, output).
* **OpenAI**: the GPT-5 family model pages on developers.openai.com, as
  returned by a web search; the pricing page itself could not be fetched when
  this table was written. Only models whose prices were confirmed that way
  are listed (GPT-4.1 and GPT-4o were left out: the search results conflicted).
  Every other OpenAI model reports an unknown cost until it is added here or
  in an override file.

Bedrock regional endpoints
--------------------------
From Claude Sonnet 4.5 / Haiku 4.5 / Opus 4.5 on, and for Amazon Nova 2 Lite,
Bedrock charges 10% more for in-region and geographic cross-region
(``us.``/``eu.``/``apac.``/...) endpoints than for the ``global.`` inference
profile. Those entries carry ``regional_multiplier=1.1``, applied to every
model id that does not start with ``global.``.

Overriding
----------
Set ``NIMBUS_PRICING_FILE`` to a JSON file to add models or replace these
prices without a code change::

    {
      "models": {
        "bedrock:amazon.nova-pro-v1:0": {"input": 0.8, "output": 3.2, "cache_read": 0.2},
        "openai:gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "my-custom-model": {"input": 1, "output": 2},
        "anthropic:claude-opus-4-1": null
      }
    }

Keys are ``provider:model_id`` or a bare ``model_id`` (any provider), matched
against the id as given and against its normalized form (see
:func:`normalize_model_id`). Prices are USD per 1M tokens. ``null`` marks a
model as unpriced. The override wins over the built-in table. The file must be
present wherever runs execute, which for a cloud evaluation is the worker.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The date the built-in table was checked against its sources.
PRICING_AS_OF = "2026-09-27"
#: Environment variable naming an optional JSON override file.
PRICING_FILE_ENV = "NIMBUS_PRICING_FILE"

_PER_TOKEN = 1_000_000


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens.

    ``cache_read`` / ``cache_write`` default to the input price when a model
    publishes none, so cached tokens are never counted as free.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    #: Bedrock only: the premium for non-``global.`` endpoints.
    regional_multiplier: float = 1.0

    def scaled(self, factor: float) -> Price:
        def scale(value: float | None) -> float | None:
            return None if value is None else value * factor

        return Price(
            input=self.input * factor,
            output=self.output * factor,
            cache_read=scale(self.cache_read),
            cache_write=scale(self.cache_write),
        )


FREE = Price(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0)

# --------------------------------------------------------------------------- #
# The built-in table
# --------------------------------------------------------------------------- #

#: Claude models by family, shared by the Anthropic API and Bedrock (whose
#: ``global.`` endpoint lists the same prices). ``cache_write`` is the 5-minute
#: cache write. The ids from both providers normalize to these keys.
ANTHROPIC_PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, 0.25, 12.5, regional_multiplier=1.1),
    "claude-mythos-5-1": Price(10.0, 50.0, 0.25, 12.5, regional_multiplier=1.1),
    "claude-fable-5": Price(10.0, 50.0, 1.0, 12.5, regional_multiplier=1.1),
    "claude-mythos-5": Price(10.0, 50.0, 1.0, 12.5, regional_multiplier=1.1),
    "claude-opus-5-5": Price(4.0, 20.0, 0.2, 5.0, regional_multiplier=1.1),
    "claude-opus-5": Price(5.0, 25.0, 0.5, 6.25, regional_multiplier=1.1),
    "claude-opus-4-8": Price(5.0, 25.0, 0.5, 6.25, regional_multiplier=1.1),
    "claude-opus-4-7": Price(5.0, 25.0, 0.5, 6.25, regional_multiplier=1.1),
    "claude-opus-4-6": Price(5.0, 25.0, 0.5, 6.25, regional_multiplier=1.1),
    "claude-opus-4-5": Price(5.0, 25.0, 0.5, 6.25, regional_multiplier=1.1),
    "claude-opus-4-1": Price(15.0, 75.0, 1.5, 18.75),
    "claude-opus-4": Price(15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-5": Price(2.0, 10.0, 0.2, 2.5, regional_multiplier=1.1),
    "claude-sonnet-4-6": Price(3.0, 15.0, 0.3, 3.75, regional_multiplier=1.1),
    "claude-sonnet-4-5": Price(3.0, 15.0, 0.3, 3.75, regional_multiplier=1.1),
    "claude-sonnet-4": Price(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.1, 1.25, regional_multiplier=1.1),
    "claude-3-7-sonnet": Price(3.0, 15.0, 0.3, 3.75),
    "claude-3-5-sonnet": Price(3.0, 15.0, 0.3, 3.75),
    "claude-3-5-haiku": Price(0.8, 4.0, 0.08, 1.0),
    "claude-3-opus": Price(15.0, 75.0),
    "claude-3-sonnet": Price(3.0, 15.0),
    "claude-3-haiku": Price(0.25, 1.25),
}

#: Non-Anthropic Bedrock models, keyed by normalized model id (no region
#: prefix, no ``:N`` suffix, no trailing ``-vN``). us-east-1 on-demand prices.
BEDROCK_PRICES: dict[str, Price] = {
    # Amazon Nova. Cache writes are free on Nova; cache reads are discounted.
    "amazon.nova-micro": Price(0.035, 0.14, 0.00875, 0.0),
    "amazon.nova-lite": Price(0.06, 0.24, 0.015, 0.0),
    "amazon.nova-pro": Price(0.8, 3.2, 0.2, 0.0),
    "amazon.nova-premier": Price(2.5, 12.5, 0.625, 0.0),
    "amazon.nova-2-lite": Price(0.3, 2.5, 0.075, 0.0, regional_multiplier=1.1),
    # Meta Llama
    "meta.llama3-8b-instruct": Price(0.3, 0.6),
    "meta.llama3-70b-instruct": Price(2.65, 3.5),
    "meta.llama3-1-8b-instruct": Price(0.22, 0.22),
    "meta.llama3-1-70b-instruct": Price(0.72, 0.72),
    "meta.llama3-2-1b-instruct": Price(0.1, 0.1),
    "meta.llama3-2-3b-instruct": Price(0.15, 0.15),
    "meta.llama3-2-11b-instruct": Price(0.16, 0.16),
    "meta.llama3-2-90b-instruct": Price(0.72, 0.72),
    "meta.llama3-3-70b-instruct": Price(0.72, 0.72),
    "meta.llama4-scout-17b-instruct": Price(0.17, 0.66),
    "meta.llama4-maverick-17b-instruct": Price(0.24, 0.97),
    # Mistral
    "mistral.mistral-7b-instruct": Price(0.15, 0.2),
    "mistral.mixtral-8x7b-instruct": Price(0.45, 0.7),
    "mistral.mistral-small-2402": Price(1.0, 3.0),
    "mistral.mistral-large-2402": Price(4.0, 12.0),
    "mistral.pixtral-large-2502": Price(2.0, 6.0),
    "mistral.mistral-large-3-675b-instruct": Price(0.5, 1.5),
    "mistral.ministral-3-3b-instruct": Price(0.1, 0.1),
    "mistral.ministral-3-8b-instruct": Price(0.15, 0.15),
    "mistral.ministral-3-14b-instruct": Price(0.2, 0.2),
    "mistral.magistral-small-2509": Price(0.5, 1.5),
    "mistral.devstral-2-123b": Price(0.4, 2.0),
    # Others
    "deepseek.r1": Price(1.35, 5.4),
    "openai.gpt-oss-20b-1": Price(0.07, 0.3),
    "openai.gpt-oss-120b-1": Price(0.15, 0.6),
    "cohere.command-r": Price(0.5, 1.5),
    "cohere.command-r-plus": Price(3.0, 15.0),
    "ai21.jamba-1-5-mini": Price(0.2, 0.4),
    "ai21.jamba-1-5-large": Price(2.0, 8.0),
}

#: OpenAI API models, keyed by id without a trailing ``-YYYY-MM-DD`` snapshot.
OPENAI_PRICES: dict[str, Price] = {
    "gpt-5": Price(1.25, 10.0, 0.125),
    "gpt-5-mini": Price(0.25, 2.0, 0.025),
    "gpt-5-nano": Price(0.05, 0.4, 0.005),
    "gpt-5.1": Price(1.25, 10.0, 0.125),
    "gpt-5.2": Price(1.75, 14.0, 0.175),
}

# --------------------------------------------------------------------------- #
# Model id normalization
# --------------------------------------------------------------------------- #

#: Cross-region inference-profile prefixes. ``global.`` is the only one priced
#: at the base rate.
_REGION_PREFIX = re.compile(r"^(global|us-gov|us|eu|apac|jp|au|ca|sa|me|af|mx)\.")
_BEDROCK_VERSION = re.compile(r"-v\d+$")
_ANTHROPIC_DATE = re.compile(r"-\d{8}$")
_OPENAI_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _bedrock_base(model_id: str) -> str:
    """``arn:...:inference-profile/us.anthropic.x-v1:0`` -> ``us.anthropic.x-v1:0``."""
    return model_id.rsplit("/", 1)[-1] if model_id.startswith("arn:") else model_id


def is_global_endpoint(model_id: str) -> bool:
    """Whether a Bedrock model id routes through the ``global.`` inference profile."""
    return _bedrock_base(model_id).startswith("global.")


def _claude_family(model_id: str) -> str:
    """``claude-sonnet-4-5-20250929`` / ``anthropic.claude-sonnet-4-5-20250929-v1`` -> family."""
    name = model_id.removeprefix("anthropic.")
    name = _BEDROCK_VERSION.sub("", name)
    return _ANTHROPIC_DATE.sub("", name)


def normalize_model_id(provider: str, model_id: str) -> str:
    """The key ``model_id`` is looked up under in the built-in table.

    Bedrock: drop an ARN's path, a region prefix (``us.``, ``global.``, ...),
    the ``:N`` throughput suffix and a trailing ``-vN``; a Claude model becomes
    its family (``claude-sonnet-4-5``). Anthropic: drop the ``-YYYYMMDD``
    snapshot date. OpenAI: drop the ``-YYYY-MM-DD`` snapshot date.
    """
    model_id = model_id.strip()
    match provider:
        case "bedrock":
            name = _REGION_PREFIX.sub("", _bedrock_base(model_id)).split(":", 1)[0]
            if name.startswith("anthropic."):
                return _claude_family(name)
            return _BEDROCK_VERSION.sub("", name)
        case "anthropic":
            return _claude_family(model_id)
        case "openai":
            return _OPENAI_SNAPSHOT.sub("", model_id)
        case _:
            return model_id


# --------------------------------------------------------------------------- #
# Override file
# --------------------------------------------------------------------------- #

_override_cache: tuple[str, float, dict[str, Price | None]] | None = None


def _parse_price(key: str, value: Any) -> Price | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{key}: expected an object or null")
    fields: dict[str, float | None] = {}
    for name in ("input", "output", "cache_read", "cache_write"):
        raw = value.get(name)
        if raw is None:
            if name in ("input", "output"):
                raise ValueError(f"{key}: {name!r} is required")
            fields[name] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, int | float) or raw < 0:
            raise ValueError(f"{key}: {name!r} must be a non-negative number")
        fields[name] = float(raw)
    return Price(**fields)  # type: ignore[arg-type]


def load_overrides(path: str | None = None) -> dict[str, Price | None]:
    """The override file's prices, or ``{}`` when there is none (or it is broken).

    Re-read whenever the file changes. A broken file is logged and ignored:
    a pricing typo must not fail the run it would have priced.
    """
    global _override_cache
    path = path if path is not None else os.environ.get(PRICING_FILE_ENV)
    if not path:
        return {}
    try:
        mtime = Path(path).stat().st_mtime
    except OSError as exc:
        logger.warning("%s=%s is unreadable: %s", PRICING_FILE_ENV, path, exc)
        return {}
    if _override_cache is not None and _override_cache[:2] == (path, mtime):
        return _override_cache[2]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        models = data.get("models") if isinstance(data, Mapping) else None
        if not isinstance(models, Mapping):
            raise ValueError('expected {"models": {...}} at the top level')
        parsed = {str(key): _parse_price(str(key), value) for key, value in models.items()}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring pricing file %s: %s", path, exc)
        return {}
    _override_cache = (path, mtime, parsed)
    return parsed


def reset_cache() -> None:
    """Forget the parsed override file (tests)."""
    global _override_cache
    _override_cache = None


# --------------------------------------------------------------------------- #
# Lookup and arithmetic
# --------------------------------------------------------------------------- #

_MISSING = object()


def _from_overrides(provider: str, model_id: str) -> Price | None | object:
    overrides = load_overrides()
    if not overrides:
        return _MISSING
    normalized = normalize_model_id(provider, model_id)
    for key in (
        f"{provider}:{model_id}",
        f"{provider}:{normalized}",
        model_id,
        normalized,
    ):
        if key in overrides:
            return overrides[key]
    return _MISSING


def _builtin(provider: str, model_id: str) -> Price | None:
    normalized = normalize_model_id(provider, model_id)
    match provider:
        case "ollama":
            return FREE
        case "bedrock":
            price = ANTHROPIC_PRICES.get(normalized) or BEDROCK_PRICES.get(normalized)
            if price is None:
                return None
            if is_global_endpoint(model_id):
                return replace(price, regional_multiplier=1.0)
            return price.scaled(price.regional_multiplier)
        case "anthropic":
            return ANTHROPIC_PRICES.get(normalized)
        case "openai":
            return OPENAI_PRICES.get(normalized)
        case _:
            return None


def lookup(provider: str, model_id: str) -> Price | None:
    """The effective price of ``model_id`` on ``provider``, or ``None`` if unknown."""
    found = _from_overrides(provider, model_id)
    if found is not _MISSING:
        return found  # type: ignore[return-value]
    return _builtin(provider, model_id)


def is_priced(provider: str, model_id: str) -> bool:
    return lookup(provider, model_id) is not None


def cost_usd(provider: str, model_id: str, usage: Mapping[str, Any] | None) -> float | None:
    """Estimated USD for ``usage`` on this model, or ``None`` when it has no price.

    ``usage`` is a run's ``metrics`` shape: ``input_tokens``, ``output_tokens``
    and optionally ``cache_read_input_tokens`` / ``cache_write_input_tokens``.
    OpenAI counts cached tokens *inside* ``input_tokens``; Bedrock and
    Anthropic report them separately. Either way each token is priced once.
    """
    price = lookup(provider, model_id)
    if price is None:
        return None
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_write_input_tokens") or 0)
    if provider == "openai":
        input_tokens = max(0, input_tokens - cache_read)
    read_rate = price.input if price.cache_read is None else price.cache_read
    write_rate = price.input if price.cache_write is None else price.cache_write
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * read_rate
        + cache_write * write_rate
    ) / _PER_TOKEN
    return round(total, 6)
