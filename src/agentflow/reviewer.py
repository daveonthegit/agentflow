"""Deterministic gate logic and change-detection for the read-only reviewer.

The reviewer is a non-deterministic Agent Role, so a unit test cannot pin its
judgement directly. This module isolates the two reviewer concerns that *are*
deterministic and safety-critical, so both can be regression-tested:

1. :func:`gate_decision` — how a validated review maps to the workflow gate
   (approve vs. block). The evaluation fixtures in
   ``tests/fixtures/reviewer_eval/`` exercise this against known-good and
   known-bad candidates via ``tests/test_reviewer_eval.py``.
2. :func:`reviewer_fingerprint` — a stable digest of the reviewer prompt and
   its per-adapter model routing. The fixture suite pins this digest, so any
   change to the reviewer prompt or model fails the suite until the fixtures
   have been re-reviewed and the pinned digest refreshed.
"""

from __future__ import annotations

import hashlib
import json

from .agent_adapter import ROLE_INSTRUCTIONS, SUGGESTED_MODELS
from .contracts import validate_review

# The workflow gate outcomes a review can resolve to.
GATE_APPROVE = "approve"
GATE_BLOCKED = "blocked"


def gate_decision(review: object) -> str:
    """Resolve a review to its workflow gate outcome.

    A review blocks the workflow when its disposition is not ``approve`` or when
    it carries any ``blocker`` finding; a ``blocker`` overrides an ``approve``
    disposition so a review cannot approve past a defect it also reports. The
    review is validated first so the gate never runs on a malformed object.
    """
    validated = validate_review(review)
    has_blocker = any(
        finding["severity"] == "blocker" for finding in validated["findings"]
    )
    if validated["disposition"] != "approve" or has_blocker:
        return GATE_BLOCKED
    return GATE_APPROVE


def reviewer_fingerprint() -> str:
    """Digest the reviewer prompt and model routing that shape its judgement.

    Changing the reviewer instructions or any adapter's reviewer model shifts
    this digest, which the fixture suite pins. That forces a re-review of the
    evaluation fixtures before the reviewer's behaviour is allowed to change.
    """
    material = {
        "instructions": ROLE_INSTRUCTIONS["reviewer"],
        "models": {
            adapter: models["reviewer"]
            for adapter, models in SUGGESTED_MODELS.items()
        },
    }
    payload = json.dumps(material, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
