"""Configuration for the Ledger privacy router middleware.

The same object is built twice:

* from the `google.protobuf.Struct` the gateway passes to `ValidateConfig`,
  before it accepts a policy, and
* from the `Struct` attached to every `EvaluateHttpRequest` call.

Keeping one schema for both means a policy that the gateway accepts cannot
fail on its first real request because the two paths disagree.

The config is deliberately `extra="forbid"`. A typo in a policy file is a
silent no-op otherwise: `deny_threshold: 30` would be ignored, and the
router would look configured while doing nothing. A rejected config is a
policy that never gets applied, which is the safe failure.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from privacy_router.injection_guard import GuardPolicy

__all__ = ["RouterConfig"]


class RouterConfig(BaseModel):
    """One policy's router configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["redact", "deny"] = "redact"
    """`redact` masks personal detail; `deny` refuses the request instead."""

    redact_models: list[str] = Field(default_factory=list)
    """Model IDs whose request bodies must be redacted."""

    redact_hosts: list[str] = Field(default_factory=lambda: ["api.tavily.com"])
    """Hosts whose request bodies must be redacted."""

    allowed_hosts: list[str] = Field(default_factory=list)
    """The network policy's allowlist. An empty list means "do not check"."""

    task_hosts: list[str] = Field(default_factory=list)
    """Hosts this task legitimately talks to. Empty means every allowed host."""

    deny_hosts: list[str] = Field(default_factory=list)
    """Hosts that must never receive personal detail."""

    third_party_hosts: list[str] = Field(default_factory=lambda: ["api.tavily.com"])
    """Hosts where a bulk dump of personal detail is not a query."""

    ocr_models: list[str] = Field(default_factory=list)
    """Models allowed to receive a document image."""

    forbidden_flags: list[str] = Field(
        default_factory=lambda: ["include_answer", "include_raw_content"]
    )
    """Third-party flags that turn a lookup into a model echo."""

    deny_threshold: int = Field(default=3, ge=1, le=20)
    """Distinct categories that make a third-party request a bulk dump."""

    escalate: bool = False
    """Call Nemotron Nano for residual spans. Off by default: the Nano round
    trip has to fit inside the middleware timeout, and the deterministic pass
    already covers the structured identifiers."""

    max_escalation_bytes: int = Field(default=16_384, ge=0)
    """Do not escalate a body larger than this. The deterministic pass is
    free; the model call is not, and a large body is a latency risk."""

    timeout_ms: int = Field(default=1_500, ge=50, le=30_000)
    """The router's own budget for one evaluation, including escalation."""

    max_payload_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    """The largest body the service advertises. OpenShell caps this again at
    the registration's `max_payload_bytes`."""

    names_file: str | None = None
    """A host-side file of household names, one per line, mode 0600. It is
    deliberately not a config list: the policy file is committed, and the
    household's names are not."""

    audit_log_path: str = "/var/log/ledger/audit.jsonl"
    """Append-only JSONL audit trail, written on the service host."""

    @field_validator("deny_threshold")
    @classmethod
    def _threshold_is_sane(cls, value: int) -> int:
        if value < 1:
            raise ValueError("deny_threshold must be at least 1")
        return value

    def as_guard_policy(self) -> GuardPolicy:
        """The guard's view of this config."""
        return GuardPolicy(
            allowed_hosts=tuple(self.allowed_hosts),
            task_hosts=tuple(self.task_hosts),
            deny_hosts=tuple(self.deny_hosts),
            third_party_hosts=tuple(self.third_party_hosts),
            ocr_models=tuple(self.ocr_models),
            forbidden_flags=tuple(self.forbidden_flags),
            deny_threshold=self.deny_threshold,
        )

    def redacts_host(self, host: str) -> bool:
        return host.strip().lower() in {h.strip().lower() for h in self.redact_hosts}

    def redacts_model(self, model: str) -> bool:
        return model.strip().lower() in {m.strip().lower() for m in self.redact_models}
