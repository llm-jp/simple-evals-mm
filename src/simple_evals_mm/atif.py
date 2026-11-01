"""ATIF-v1.8 (Harbor's Agent Trajectory Interchange Format): the one record every agent produces per trial.

Dataclasses with to_dict / from_dict (None fields are dropped), a light validator, and the image store: images are
written once to `<job dir>/images/<sha256>.<ext>` and referenced by a path relative to the job dir, so trajectories
contain no base64 and no absolute paths. Every dataclass carries `extra` for fields ATIF does not define
(finish_reason, provider, failure cause, ...).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import threading
from dataclasses import dataclass, field, fields
from typing import Any

SCHEMA_VERSION = "ATIF-v1.8"
SOURCES = ("system", "user", "agent")
AGENT_ONLY = ("model_name", "reasoning_effort", "reasoning_content", "tool_calls", "metrics", "is_copied_context")


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


class _Base:
    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            if isinstance(v, _Base):
                v = v.to_dict()
            elif isinstance(v, list):
                v = [x.to_dict() if isinstance(x, _Base) else x for x in v]
            out[f.name] = v
        return _drop_none(out)


@dataclass
class ImageSource(_Base):
    media_type: str
    path: str  # relative to the job dir: images/<sha256>.<ext>


@dataclass
class ContentPart(_Base):
    type: str  # "text" | "image"
    text: str | None = None
    source: ImageSource | None = None

    @classmethod
    def from_dict(cls, d: dict) -> ContentPart:
        src = d.get("source")
        return cls(type=d["type"], text=d.get("text"), source=ImageSource(**src) if src else None)


@dataclass
class ToolCall(_Base):
    tool_call_id: str
    function_name: str
    arguments: Any
    extra: dict | None = None


@dataclass
class ObservationResult(_Base):
    source_call_id: str | None = None
    content: str | list | None = None
    extra: dict | None = None


@dataclass
class Observation(_Base):
    results: list[ObservationResult] = field(default_factory=list)
    extra: dict | None = None


@dataclass
class Metrics(_Base):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    extra: dict | None = None  # reasoning_tokens, ...


@dataclass
class FinalMetrics(_Base):
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    total_steps: int | None = None
    extra: dict | None = None


@dataclass
class Agent(_Base):
    name: str
    version: str
    model_name: str | None = None
    extra: dict | None = None


@dataclass
class Step(_Base):
    step_id: int
    source: str  # system / user / agent
    message: str | list[ContentPart] = ""
    timestamp: str | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    observation: Observation | None = None
    metrics: Metrics | None = None
    # an agent step the model did not generate: context supplied by the task (a dataset's assistant turn)
    is_copied_context: bool | None = None
    extra: dict | None = None

    @property
    def text(self) -> str:
        if isinstance(self.message, str):
            return self.message
        return "".join(p.text or "" for p in self.message if p.type == "text")

    @classmethod
    def from_dict(cls, d: dict) -> Step:
        msg = d.get("message", "")
        if isinstance(msg, list):
            msg = [ContentPart.from_dict(p) for p in msg]
        obs = d.get("observation")
        return cls(
            step_id=d["step_id"], source=d["source"], message=msg, timestamp=d.get("timestamp"),
            model_name=d.get("model_name"), reasoning_effort=d.get("reasoning_effort"),
            reasoning_content=d.get("reasoning_content"),
            tool_calls=[ToolCall(**t) for t in d["tool_calls"]] if d.get("tool_calls") is not None else None,
            observation=Observation([ObservationResult(**r) for r in obs.get("results", [])], obs.get("extra")) if obs else None,
            metrics=Metrics(**d["metrics"]) if d.get("metrics") else None,
            is_copied_context=d.get("is_copied_context"), extra=d.get("extra"),
        )


@dataclass
class Trajectory(_Base):
    session_id: str  # the job UUID
    agent: Agent
    trajectory_id: str | None = None  # the trial id: <task_id>__a<k>
    steps: list[Step] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    notes: str | None = None
    final_metrics: FinalMetrics | None = None
    extra: dict | None = None  # task_id, dataset, attempt, failure {cause, error, step}, duration_seconds, ...

    @classmethod
    def from_dict(cls, d: dict) -> Trajectory:
        return cls(
            session_id=d["session_id"], agent=Agent(**d["agent"]), trajectory_id=d.get("trajectory_id"), steps=[Step.from_dict(s) for s in d.get("steps", [])],
            schema_version=d.get("schema_version", SCHEMA_VERSION), notes=d.get("notes"),
            final_metrics=FinalMetrics(**d["final_metrics"]) if d.get("final_metrics") else None, extra=d.get("extra"),
        )

    # ---- views used by graders
    @property
    def agent_steps(self) -> list[Step]:
        """The model's own answers (copied context excluded)."""
        return [s for s in self.steps if s.source == "agent" and not s.is_copied_context]

    @property
    def final_text(self) -> str:
        """The last answer of the model ("" when the trial failed before answering)."""
        a = self.agent_steps
        return a[-1].text if a else ""

    @property
    def task_id(self) -> str:
        return (self.extra or {}).get("task_id") or (self.trajectory_id or "").rsplit("__a", 1)[0]

    @property
    def failure(self) -> dict | None:
        return (self.extra or {}).get("failure")


def validate(traj: Trajectory | dict) -> list[str]:
    """Problems of a trajectory (empty list = valid): schema version, required fields, sequential step ids from 1,
    known sources, agent-only fields only on agent steps, image parts with a relative source path."""
    d = traj.to_dict() if isinstance(traj, Trajectory) else traj
    errs = []
    if d.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version {d.get('schema_version')!r} != {SCHEMA_VERSION!r}")
    for k in ("session_id", "trajectory_id"):
        if not d.get(k):
            errs.append(f"missing {k}")
    agent = d.get("agent") or {}
    for k in ("name", "version"):
        if not agent.get(k):
            errs.append(f"missing agent.{k}")
    for i, s in enumerate(d.get("steps") or [], 1):
        if s.get("step_id") != i:
            errs.append(f"step {i}: step_id {s.get('step_id')} is not sequential")
        if s.get("source") not in SOURCES:
            errs.append(f"step {i}: source {s.get('source')!r} not in {SOURCES}")
        if s.get("source") != "agent":
            bad = [k for k in AGENT_ONLY if s.get(k) is not None]
            if bad:
                errs.append(f"step {i}: agent-only fields on a {s.get('source')} step: {bad}")
        if "message" not in s:
            errs.append(f"step {i}: missing message")
        for p in s.get("message") if isinstance(s.get("message"), list) else []:
            if p.get("type") == "image":
                path = (p.get("source") or {}).get("path") or ""
                if not path or os.path.isabs(path) or path.startswith("data:"):
                    errs.append(f"step {i}: image source path must be relative to the job dir, got {path[:60]!r}")
            elif p.get("type") != "text":
                errs.append(f"step {i}: unknown content part type {p.get('type')!r}")
    return errs


class ImageStore:
    """Writes each image once to <job_dir>/images/<sha256>.<ext>; returns the job-relative path."""

    def __init__(self, job_dir: str):
        self.job_dir = job_dir
        self._lock = threading.Lock()
        self._seen: dict[str, str] = {}  # source path -> relative path (dataset images repeat across attempts)

    def put(self, path: str | None = None, data: bytes | None = None, media_type: str | None = None) -> ImageSource:
        if path is not None and path in self._seen:
            rel = self._seen[path]
            return ImageSource(media_type=media_type or _media_type(rel), path=rel)
        if data is None:
            with open(path, "rb") as f:  # type: ignore[arg-type]
                data = f.read()
        media_type = media_type or (_media_type(path) if path else "image/png")
        ext = mimetypes.guess_extension(media_type) or ".bin"
        ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
        rel = f"images/{hashlib.sha256(data).hexdigest()}{ext}"
        dest = os.path.join(self.job_dir, rel)
        with self._lock:
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".part", "wb") as f:
                    f.write(data)
                os.replace(dest + ".part", dest)
            if path is not None:
                self._seen[path] = rel
        return ImageSource(media_type=media_type, path=rel)


def _media_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
