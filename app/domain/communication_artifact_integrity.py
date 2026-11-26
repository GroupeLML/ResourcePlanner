"""Inactive ADR-039 final artifact integrity, version 1.

Only trusted server generation may supply GeneratedMessageContent at issuance.
The review input deliberately has no HTML field. On restoration the independent
expected fingerprint must come from a trusted artifact record, not the payload.
No database, rendering of frozen content, approval or transport is performed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import re

from .communication_actual_message_recipients import ActualMessageComposition
from .communication_message_context import MessageContextEvidence, _canonical, _json
from .communication_recipient_policy import _require_keys
from .project_communication_messages import (
    project_projection_fingerprint, render_project_review_html, serialize_project_projection,
)


def _digest(kind, payload):
    return hashlib.sha256(_canonical(["adr039", kind, 1, payload]).encode("utf-8")).hexdigest()


def _fingerprint(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Missing or malformed trusted integrity fingerprint")
    return value


def _text(value):
    if type(value) is not str or not value.strip():
        raise ValueError("Included message requires nonblank subject, text and HTML")


def _read_record(cls, payload, keys):
    return cls(**_require_keys(payload, keys))


@dataclass(frozen=True, slots=True)
class MessageEnvelopeIntegrity:
    message_key: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ActualCompositionIntegrity:
    """Recomputed actual composition, not the provisional digest or P5."""
    context: MessageContextEvidence
    composition: ActualMessageComposition = field(init=False)
    fingerprint: str = field(init=False)
    current_snapshot_integrity: str = field(init=False)
    baseline_snapshot_integrity: str | None = field(init=False)
    envelopes: tuple[MessageEnvelopeIntegrity, ...] = field(init=False)

    def __post_init__(self):
        composition = ActualMessageComposition(self.context)
        policy = self.context.current_policy.to_payload()
        baseline = self.context.baseline
        authority = {
            "current_p5": project_projection_fingerprint(self.context.current),
            "current_snapshot_integrity": _digest("snapshot", serialize_project_projection(self.context.current)),
            "baseline_snapshot_integrity": None if baseline is None else
                _digest("snapshot", serialize_project_projection(baseline.snapshot)),
            "generator": policy["generator"],
            "generator_identity_fingerprint": policy["generator_identity_fingerprint"],
            "composition_identities": policy["composition_identities"],
            "actual_composition": composition.to_payload(),
        }
        fingerprint = _digest("actual_composition", authority)
        envelopes = tuple(MessageEnvelopeIntegrity(
            row.message_key, row.to_addresses, row.cc_addresses,
            _digest("message_envelope", {
                "message_key": row.message_key,
                "composition_fingerprint": fingerprint,
                "to": row.to_addresses, "cc": row.cc_addresses,
                "recipient_evidence": row.to_payload(),
            }),
        ) for row in composition.messages)
        object.__setattr__(self, "composition", composition)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "current_snapshot_integrity", authority["current_snapshot_integrity"])
        object.__setattr__(self, "baseline_snapshot_integrity", authority["baseline_snapshot_integrity"])
        object.__setattr__(self, "envelopes", envelopes)

    def to_payload(self):
        return _json({
            "representation": "adr039_actual_composition_integrity_v1",
            "current_p5": project_projection_fingerprint(self.context.current),
            "current_snapshot_integrity": self.current_snapshot_integrity,
            "baseline_snapshot_integrity": self.baseline_snapshot_integrity,
            "composition": self.composition.to_payload(),
            "fingerprint": self.fingerprint,
            "envelopes": [asdict(row) for row in self.envelopes],
        })


@dataclass(frozen=True, slots=True)
class GeneratedMessageContent:
    """Trusted server-generated pair, bound to one actual envelope at issuance.

    This is a server-side authority input, not a client request schema. This
    inactive checkpoint does not implement the operational generator.
    """
    message_key: str
    envelope_fingerprint: str
    subject: str
    body: str
    body_html: str

    def __post_init__(self):
        _fingerprint(self.envelope_fingerprint)
        for value in (self.message_key, self.subject, self.body, self.body_html):
            _text(value)


@dataclass(frozen=True, slots=True)
class MessageTextReview:
    message_key: str
    subject: str | None = None
    body: str | None = None

    def __post_init__(self):
        _text(self.message_key)
        if any(value is not None and type(value) is not str for value in (self.subject, self.body)):
            raise ValueError("Malformed text review")


@dataclass(frozen=True, slots=True)
class FrozenReviewedContent:
    generated: GeneratedMessageContent
    review_explicit: bool
    subject: str
    body: str
    body_html: str
    html_origin: str

    def __post_init__(self):
        if type(self.generated) is not GeneratedMessageContent or type(self.review_explicit) is not bool:
            raise ValueError("Missing generated content or review evidence")
        for value in (self.subject, self.body, self.body_html):
            _text(value)
        unchanged_body = self.body == self.generated.body
        if unchanged_body:
            if self.html_origin != "GENERATED" or self.body_html != self.generated.body_html:
                raise ValueError("Unchanged text must retain the generated HTML")
        elif self.html_origin != "SERVER_REVIEW_RENDERED" or not self.review_explicit:
            raise ValueError("Edited text requires explicit server-rendered review")
        if not self.review_explicit and (self.subject != self.generated.subject or not unchanged_body):
            raise ValueError("Unreviewed content cannot differ from generation")

    @classmethod
    def capture(cls, generated, review=None):
        if type(generated) is not GeneratedMessageContent:
            raise ValueError("Expected trusted generated content")
        if review is not None and (type(review) is not MessageTextReview or review.message_key != generated.message_key):
            raise ValueError("Review message identity mismatch")
        subject = generated.subject if review is None or review.subject is None else review.subject.strip()
        body = generated.body if review is None or review.body is None else review.body.strip()
        _text(subject)
        _text(body)
        unchanged = body == generated.body
        html = generated.body_html if unchanged else render_project_review_html(body)
        return cls(generated, review is not None, subject, body, html,
                   "GENERATED" if unchanged else "SERVER_REVIEW_RENDERED")

    @property
    def fingerprint(self):
        return _digest("reviewed_message_content", asdict(self))

    def to_payload(self):
        return {**asdict(self), "fingerprint": self.fingerprint}


def _by_key(rows, row_type):
    if type(rows) is not tuple or any(type(row) is not row_type for row in rows):
        raise ValueError("Expected immutable message content records")
    result = {}
    for row in rows:
        if row.message_key in result:
            raise ValueError("Duplicate message content key")
        result[row.message_key] = row
    return result


@dataclass(frozen=True, slots=True, init=False)
class PreparedArtifactIntegrity:
    """Inactive preparation candidate. Not an approved or delivered artifact.

    Issue only through capture() with trusted server generation. Restore only
    through from_payload()/deserialize() with independently trusted integrity.
    The hash is not authentication and cannot detect replacement of its trust root.
    """
    integrity: ActualCompositionIntegrity
    contents: tuple[FrozenReviewedContent, ...]

    def __init__(self):
        raise TypeError("Use trusted capture or independently verified restoration")

    @classmethod
    def _assemble(cls, integrity, contents):
        result = object.__new__(cls)
        object.__setattr__(result, "integrity", integrity)
        object.__setattr__(result, "contents", contents)
        result._validate()
        return result

    def _validate(self):
        if type(self.integrity) is not ActualCompositionIntegrity:
            raise ValueError("Expected authoritative actual composition")
        composition = self.integrity.composition
        if not composition.messages:
            raise ValueError("No included messages for preparation")
        for row in composition.messages:
            if (not row.mandatory_complete or row.binding_diagnostic is not None
                    or not row.to_addresses
                    or any(d.severity == "BLOCKING" for d in row.existing_diagnostics)):
                raise ValueError("Blocking recipient evidence for " + row.message_key)
        if (type(self.contents) is not tuple or any(type(row) is not FrozenReviewedContent for row in self.contents)
                or tuple(row.generated.message_key for row in self.contents) != composition.included_message_keys):
            raise ValueError("Reviewed content coverage must match included messages")
        for content, envelope in zip(self.contents, self.integrity.envelopes, strict=True):
            if content.generated.envelope_fingerprint != envelope.fingerprint:
                raise ValueError("Reviewed content envelope mismatch")

    @classmethod
    def capture(cls, *, context, generated, reviews=()):
        integrity = ActualCompositionIntegrity(context)
        by_key = _by_key(generated, GeneratedMessageContent)
        review_by_key = _by_key(reviews, MessageTextReview)
        keys = integrity.composition.included_message_keys
        if set(by_key) != set(keys) or not set(review_by_key) <= set(keys):
            raise ValueError("Generated/reviewed message coverage mismatch")
        return cls._assemble(integrity, tuple(
            FrozenReviewedContent.capture(by_key[key], review_by_key.get(key)) for key in keys
        ))

    def _payload(self):
        return _json({
            "representation": "adr039_prepared_artifact_integrity_v1",
            "actual_integrity": self.integrity.to_payload(),
            "contents": [row.to_payload() for row in self.contents],
        })

    @property
    def fingerprint(self):
        return _digest("prepared_artifact", self._payload())

    def to_payload(self):
        return {**self._payload(), "fingerprint": self.fingerprint}

    def serialize(self):
        return _canonical(self.to_payload())

    @classmethod
    def from_payload(cls, payload, *, context, expected_fingerprint):
        expected = _fingerprint(expected_fingerprint)
        data = _require_keys(payload, {"representation", "actual_integrity", "contents", "fingerprint"})
        # Check the independent trust root before accepting any frozen HTML.
        if not hmac.compare_digest(expected, _fingerprint(data["fingerprint"])):
            raise ValueError("Prepared artifact differs from trusted integrity")
        unsigned = {key: value for key, value in data.items() if key != "fingerprint"}
        if not hmac.compare_digest(expected, _digest("prepared_artifact", unsigned)):
            raise ValueError("Prepared artifact payload integrity mismatch")
        integrity = ActualCompositionIntegrity(context)
        if _canonical(integrity.to_payload()) != _canonical(data["actual_integrity"]):
            raise ValueError("Contradictory actual composition or envelope integrity")
        if type(data["contents"]) is not list:
            raise ValueError("Malformed reviewed content coverage")
        contents = []
        for raw in data["contents"]:
            row = _require_keys(raw, {"generated", "review_explicit", "subject", "body", "body_html",
                                      "html_origin", "fingerprint"})
            generated = _read_record(GeneratedMessageContent, row["generated"],
                                     {"message_key", "envelope_fingerprint", "subject", "body", "body_html"})
            # Restore exact frozen values, not a regenerated HTML template.
            content = FrozenReviewedContent(generated, row["review_explicit"], row["subject"],
                                            row["body"], row["body_html"], row["html_origin"])
            if _canonical(content.to_payload()) != _canonical(row):
                raise ValueError("Contradictory reviewed content integrity")
            contents.append(content)
        result = cls._assemble(integrity, tuple(contents))
        if _canonical(result.to_payload()) != _canonical(data):
            raise ValueError("Contradictory prepared artifact claims")
        return result

    @classmethod
    def deserialize(cls, payload, *, context, expected_fingerprint):
        def read_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate prepared artifact key")
                result[key] = value
            return result

        if type(payload) is not str:
            raise ValueError("Expected serialized prepared artifact")
        return cls.from_payload(json.loads(payload, object_pairs_hook=read_object),
                                context=context, expected_fingerprint=expected_fingerprint)
