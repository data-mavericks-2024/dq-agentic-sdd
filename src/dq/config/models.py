"""Model identifiers — the single place any Claude model ID is written.

Nothing in Feature 1 calls a model. This module exists now, empty of callers, so that the first
code that does call one has an obvious place to look and no reason to inline a string. Constitution
§ Platform & Data Constraints requires model IDs in one configuration module, and requires the
reasoning tier to be replaceable by configuration change alone.

Feature 3 is the first consumer.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Final


class Tier(StrEnum):
    """What a call is for, rather than which model serves it."""

    #: Investigation, impact assessment, remediation reasoning.
    REASONING = "reasoning"
    #: Classification and summarisation.
    UTILITY = "utility"


#: Default model per tier. `REASONING` must be swappable to `claude-opus-5` by config alone, so
#: nothing may read this dict directly — call `model_for` instead.
_DEFAULTS: Final[dict[Tier, str]] = {
    Tier.REASONING: "claude-sonnet-5",
    Tier.UTILITY: "claude-haiku-4-5",
}

#: Environment override per tier.
_ENV_VAR: Final[dict[Tier, str]] = {
    Tier.REASONING: "DQ_MODEL_REASONING",
    Tier.UTILITY: "DQ_MODEL_UTILITY",
}


def model_for(tier: Tier) -> str:
    """Return the model ID configured for ``tier``."""
    return os.environ.get(_ENV_VAR[tier], "").strip() or _DEFAULTS[tier]
