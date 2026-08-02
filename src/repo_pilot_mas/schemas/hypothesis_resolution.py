"""Reviewed root-cause resolution state shared through the Blackboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HypothesisResolutionStatus(str, Enum):
    """Lifecycle of candidate root causes before patch generation."""

    UNRESOLVED = "unresolved"
    UNDER_REVIEW = "under_review"
    NEEDS_EVIDENCE = "needs_evidence"
    NEEDS_REVISION = "needs_revision"
    CONFLICT = "conflict"
    ACCEPTED = "accepted"
    INVALIDATED = "invalidated"
    REJECTED = "rejected"


@dataclass(slots=True)
class HypothesisResolution:
    """Deterministic state derived from Hypothesis and Review artifacts."""

    status: HypothesisResolutionStatus = HypothesisResolutionStatus.UNRESOLVED
    candidate_refs: tuple[str, ...] = ()
    accepted_refs: tuple[str, ...] = ()
    primary_ref: str | None = None
    review_refs: tuple[str, ...] = ()
    accepted_by_decision_id: str | None = None
    accepted_at_state_version: int | None = None
    invalidation_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = HypothesisResolutionStatus(self.status)
        self.candidate_refs = _unique_strings(self.candidate_refs)
        self.accepted_refs = _unique_strings(self.accepted_refs)
        self.review_refs = _unique_strings(self.review_refs)
        self.metadata = dict(self.metadata)
        if self.primary_ref is not None and self.primary_ref not in self.accepted_refs:
            raise ValueError("primary_ref must belong to accepted_refs")
        if self.status is HypothesisResolutionStatus.ACCEPTED:
            legacy = self.metadata.get("legacy_migrated") is True
            if not self.accepted_refs or self.primary_ref is None:
                raise ValueError("accepted resolution requires hypotheses and primary_ref")
            if not self.review_refs and not legacy:
                raise ValueError("accepted resolution requires reviews")
        elif self.accepted_refs or self.primary_ref is not None:
            raise ValueError("only accepted resolution may contain accepted hypotheses")

    @classmethod
    def unresolved(cls, candidate_refs: Sequence[str] = ()) -> HypothesisResolution:
        return cls(candidate_refs=_unique_strings(candidate_refs))

    @classmethod
    def legacy_accepted(cls, ref: str) -> HypothesisResolution:
        return cls(
            status=HypothesisResolutionStatus.ACCEPTED,
            candidate_refs=(ref,),
            accepted_refs=(ref,),
            primary_ref=ref,
            metadata={"legacy_migrated": True},
        )

    def observe_candidates(self, refs: Sequence[str]) -> None:
        combined = _unique_strings((*self.candidate_refs, *refs))
        if combined == self.candidate_refs:
            return
        self.candidate_refs = combined
        if self.status in {
            HypothesisResolutionStatus.REJECTED,
            HypothesisResolutionStatus.INVALIDATED,
        }:
            self.status = HypothesisResolutionStatus.UNRESOLVED
            self.invalidation_reason = None

    def mark_reviewed(
        self,
        review_ref: str,
        verdict: str,
        *,
        target_refs: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.review_refs = _unique_strings((*self.review_refs, review_ref))
        self.observe_candidates(target_refs)
        self.metadata.update(dict(metadata or {}))
        mapping = {
            "needs_more_evidence": HypothesisResolutionStatus.NEEDS_EVIDENCE,
            "changes_requested": HypothesisResolutionStatus.NEEDS_REVISION,
            "unsupported": HypothesisResolutionStatus.REJECTED,
            "conflict": HypothesisResolutionStatus.CONFLICT,
            "supported": HypothesisResolutionStatus.UNDER_REVIEW,
            "compatible": HypothesisResolutionStatus.UNDER_REVIEW,
            "approved": HypothesisResolutionStatus.UNDER_REVIEW,
        }
        self.status = mapping.get(verdict, HypothesisResolutionStatus.UNDER_REVIEW)
        self.accepted_refs = ()
        self.primary_ref = None
        self.accepted_by_decision_id = None
        self.accepted_at_state_version = None
        self.invalidation_reason = None
        self.metadata.pop("legacy_migrated", None)

    def accept(
        self,
        refs: Sequence[str],
        *,
        primary_ref: str,
        review_refs: Sequence[str],
        decision_id: str,
        state_version: int,
    ) -> None:
        normalized_refs = _unique_strings(refs)
        normalized_reviews = _unique_strings(review_refs)
        if not normalized_refs:
            raise ValueError("accepted hypothesis set must not be empty")
        if primary_ref not in normalized_refs:
            raise ValueError("primary hypothesis must belong to accepted set")
        if not normalized_reviews:
            raise ValueError("accepted hypothesis set requires at least one review")
        self.candidate_refs = _unique_strings((*self.candidate_refs, *normalized_refs))
        self.accepted_refs = normalized_refs
        self.primary_ref = primary_ref
        self.review_refs = normalized_reviews
        self.accepted_by_decision_id = decision_id
        self.accepted_at_state_version = int(state_version)
        self.invalidation_reason = None
        self.status = HypothesisResolutionStatus.ACCEPTED
        self.metadata.pop("legacy_migrated", None)

    def invalidate(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("invalidation reason must not be empty")
        self.status = HypothesisResolutionStatus.INVALIDATED
        self.accepted_refs = ()
        self.primary_ref = None
        self.accepted_by_decision_id = None
        self.accepted_at_state_version = None
        self.invalidation_reason = reason
        self.metadata.pop("legacy_migrated", None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "candidate_refs": list(self.candidate_refs),
            "accepted_refs": list(self.accepted_refs),
            "primary_ref": self.primary_ref,
            "review_refs": list(self.review_refs),
            "accepted_by_decision_id": self.accepted_by_decision_id,
            "accepted_at_state_version": self.accepted_at_state_version,
            "invalidation_reason": self.invalidation_reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HypothesisResolution:
        return cls(
            status=HypothesisResolutionStatus(str(value.get("status", "unresolved"))),
            candidate_refs=_strings(value.get("candidate_refs", ())),
            accepted_refs=_strings(value.get("accepted_refs", ())),
            primary_ref=str(value["primary_ref"]) if value.get("primary_ref") else None,
            review_refs=_strings(value.get("review_refs", ())),
            accepted_by_decision_id=(
                str(value["accepted_by_decision_id"])
                if value.get("accepted_by_decision_id")
                else None
            ),
            accepted_at_state_version=(
                int(value["accepted_at_state_version"])
                if value.get("accepted_at_state_version") is not None
                else None
            ),
            invalidation_reason=(
                str(value["invalidation_reason"])
                if value.get("invalidation_reason")
                else None
            ),
            metadata=dict(value.get("metadata", {})),
        )


def _strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(str(item) for item in value)


def _unique_strings(value: Sequence[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in value if str(item)))
