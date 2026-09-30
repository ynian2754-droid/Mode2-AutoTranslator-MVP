"""Process-local quality ownership markers and the current live progress."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QualityRuntime:
    # batch_id -> source signature, only to make a duplicate network retry
    # idempotent. Not a queue, not a scheduler.
    batch_inflight: dict[str, str] = field(default_factory=dict)
    #: In-flight manual recoveries. These are process-local concurrency
    #: markers, never a task store; each batch's retry record is durable.
    retry_inflight: set[str] = field(default_factory=set)
    retry_parallel: set[str] = field(default_factory=set)
    # prepare_id -> the project/revision/mode signature the run was frozen
    # with. It makes "one active prepare per project" real and rejects late
    # results after a project switch, a mode change or a close. Still not a
    # queue: one entry, no background execution.
    prepare_inflight: dict[str, str] = field(default_factory=dict)
    # One in-memory progress snapshot for the current confirmed prepare.
    # It is intentionally not persisted: a restarted process has no live
    # worker and the durable prepare record is normalized to interrupted.
    progress: dict[str, Any] | None = None
