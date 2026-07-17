"""Provider-neutral contracts for server-defined multi-agent applications."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import re
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import CodeType, MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from nexus_kernel.ai.contracts import (
    AgentIdentity,
    AgentToolSpec,
    ProviderId,
    TokenUsage,
    thaw_json,
)


ApplicationKind = Literal["graph", "team"]
WorkflowActionKind = Literal["input", "confirmation"]
WorkflowResultState = Literal["succeeded", "requires_action"]
WorkflowOutputFormat = Literal["text", "json"]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonSchema: TypeAlias = Mapping[str, Any] | bool
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ACTION_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _freeze_strict_json(value: Any, *, depth: int = 0) -> JsonValue:
    if depth > 32:
        raise ValueError("JSON value exceeds the maximum depth")
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType(
            {
                key: _freeze_strict_json(item, depth=depth + 1)
                for key, item in value.items()
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_strict_json(item, depth=depth + 1) for item in value)
    raise ValueError("value must be strict JSON")


def _freeze_json_schema(value: Any, *, field_name: str) -> JsonSchema:
    schema = thaw_json(_freeze_strict_json(value))
    if not isinstance(schema, dict | bool):
        raise ValueError(f"{field_name} must be a JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError(f"{field_name} must be a valid JSON Schema") from None
    frozen = _freeze_strict_json(schema)
    if not isinstance(frozen, Mapping | bool):
        raise ValueError(f"{field_name} must be a JSON Schema")
    return frozen


def _callable_semantic_value(value: Any, *, seen: frozenset[int]) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "non_finite_float"}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, CodeType):
        return _code_semantic_material(value, seen=seen)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            return {"type": _qualified_type_name(value)}
        return {
            key: _callable_semantic_value(item, seen=seen)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list | tuple):
        return [_callable_semantic_value(item, seen=seen) for item in value]
    if isinstance(value, set | frozenset):
        items = [_callable_semantic_value(item, seen=seen) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if inspect.ismodule(value):
        return {"module": getattr(value, "__name__", "")}
    if isinstance(value, type):
        return {"type": f"{value.__module__}.{value.__qualname__}"}
    if inspect.isfunction(value) or inspect.ismethod(value):
        return _callable_semantic_material(value, seen=seen)
    return {"type": _qualified_type_name(value)}


def _qualified_type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _closure_semantic_value(value: Any, *, seen: frozenset[int]) -> Any:
    if isinstance(value, list | dict | set):
        return {"mutable_type": _qualified_type_name(value)}
    return _callable_semantic_value(value, seen=seen)


def _code_semantic_material(code: CodeType, *, seen: frozenset[int]) -> dict[str, Any]:
    return {
        "argcount": code.co_argcount,
        "bytecode": code.co_code.hex(),
        "cellvars": code.co_cellvars,
        "consts": [
            _callable_semantic_value(item, seen=seen) for item in code.co_consts
        ],
        "flags": code.co_flags,
        "freevars": code.co_freevars,
        "kwonlyargcount": code.co_kwonlyargcount,
        "names": code.co_names,
        "posonlyargcount": code.co_posonlyargcount,
    }


def _callable_semantic_material(
    value: Callable[..., Any], *, seen: frozenset[int] = frozenset()
) -> dict[str, Any]:
    target = value.__func__ if inspect.ismethod(value) else value
    target_id = id(target)
    qualified_name = (
        f"{getattr(target, '__module__', '')}."
        f"{getattr(target, '__qualname__', _qualified_type_name(target))}"
    )
    if target_id in seen:
        return {"callable": qualified_name}
    nested_seen = seen | {target_id}
    try:
        source = textwrap.dedent(inspect.getsource(target))
        implementation: Any = {
            "ast": ast.dump(ast.parse(source), include_attributes=False)
        }
    except (IndentationError, OSError, SyntaxError, TypeError):
        code = getattr(target, "__code__", None)
        implementation = (
            _code_semantic_material(code, seen=nested_seen)
            if isinstance(code, CodeType)
            else {"type": _qualified_type_name(target)}
        )

    closure: dict[str, Any] = {}
    code = getattr(target, "__code__", None)
    cells = getattr(target, "__closure__", None) or ()
    if isinstance(code, CodeType):
        for name, cell in zip(code.co_freevars, cells, strict=True):
            try:
                cell_value = cell.cell_contents
            except ValueError:
                closure[name] = {"empty": True}
            else:
                closure[name] = _closure_semantic_value(
                    cell_value,
                    seen=nested_seen,
                )

    globals_material: dict[str, Any] = {}
    target_globals = getattr(target, "__globals__", None)
    if isinstance(code, CodeType) and isinstance(target_globals, dict):
        for name in sorted(set(code.co_names) & target_globals.keys()):
            global_value = target_globals[name]
            if inspect.ismodule(global_value):
                continue
            globals_material[name] = _callable_semantic_value(
                global_value,
                seen=nested_seen,
            )

    return {
        "callable": qualified_name,
        "closure": closure,
        "defaults": _callable_semantic_value(
            getattr(target, "__defaults__", None),
            seen=nested_seen,
        ),
        "globals": globals_material,
        "implementation": implementation,
        "kwdefaults": _callable_semantic_value(
            getattr(target, "__kwdefaults__", None),
            seen=nested_seen,
        ),
    }


def _callable_semantic_digest(value: Callable[..., Any]) -> str:
    material = json.dumps(
        _callable_semantic_material(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowActionSpec:
    """Public, server-owned contract for one resumable action code."""

    code: str
    kind: WorkflowActionKind
    response_schema: JsonSchema
    response_format: Literal["json_schema"] = "json_schema"

    def __post_init__(self) -> None:
        if _ACTION_CODE.fullmatch(self.code) is None:
            raise ValueError("action code must be a stable lowercase identifier")
        if self.kind not in {"input", "confirmation"}:
            raise ValueError("action kind is invalid")
        object.__setattr__(
            self,
            "response_schema",
            _freeze_json_schema(
                self.response_schema,
                field_name="action response_schema",
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowNodeResult:
    """Strict JSON output and an optional allowlisted conditional route."""

    output: JsonValue
    route: str | None = None

    def __post_init__(self) -> None:
        output = _freeze_strict_json(self.output)
        if output is None:
            raise ValueError("workflow transform output must not be null")
        if self.route is not None and _ACTION_CODE.fullmatch(self.route) is None:
            raise ValueError("workflow route must be a stable lowercase identifier")
        object.__setattr__(self, "output", output)


WorkflowTransform: TypeAlias = Callable[[JsonValue], WorkflowNodeResult]


@dataclass(frozen=True, slots=True)
class WorkflowTransformNodeSpec:
    """App-owned deterministic transformation over immutable strict JSON."""

    name: str
    transform: WorkflowTransform
    input_schema: JsonSchema = True
    output_schema: JsonSchema = True
    routes: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    max_attempts: int = 1
    _semantic_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if _ACTION_CODE.fullmatch(self.name) is None:
            raise ValueError("workflow node name must be a stable lowercase identifier")
        if not callable(self.transform):
            raise ValueError("workflow transform must be callable")
        object.__setattr__(
            self,
            "_semantic_fingerprint",
            _callable_semantic_digest(self.transform),
        )
        callable_target = getattr(self.transform, "__call__", self.transform)
        if any(
            check(self.transform) or check(callable_target)
            for check in (
                inspect.iscoroutinefunction,
                inspect.isgeneratorfunction,
                inspect.isasyncgenfunction,
            )
        ):
            raise ValueError("workflow transform must be a synchronous function")
        if len(set(self.routes)) != len(self.routes) or any(
            _ACTION_CODE.fullmatch(route) is None for route in self.routes
        ):
            raise ValueError("workflow routes must be unique stable identifiers")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("workflow node timeout must be positive and finite")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 16
        ):
            raise ValueError("workflow node max_attempts must be between 1 and 16")
        object.__setattr__(
            self,
            "input_schema",
            _freeze_json_schema(
                self.input_schema,
                field_name="workflow transform input_schema",
            ),
        )
        object.__setattr__(
            self,
            "output_schema",
            _freeze_json_schema(
                self.output_schema,
                field_name="workflow transform output_schema",
            ),
        )
        object.__setattr__(self, "routes", tuple(self.routes))
        if self.timeout_seconds is not None:
            object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True, slots=True)
class WorkflowRequestInputNodeSpec:
    """Declarative native ADK RequestInput node bound to an action allowlist."""

    name: str
    action_code: str

    def __post_init__(self) -> None:
        if _ACTION_CODE.fullmatch(self.name) is None:
            raise ValueError("workflow node name must be a stable lowercase identifier")
        if _ACTION_CODE.fullmatch(self.action_code) is None:
            raise ValueError("action code must be a stable lowercase identifier")


WorkflowFunctionNodeSpec: TypeAlias = (
    WorkflowTransformNodeSpec | WorkflowRequestInputNodeSpec
)


def _workflow_function_node_material(spec: WorkflowFunctionNodeSpec) -> dict[str, Any]:
    if isinstance(spec, WorkflowTransformNodeSpec):
        return {
            "kind": "transform",
            "name": spec.name,
            "transform": spec._semantic_fingerprint,
            "input_schema": thaw_json(spec.input_schema),
            "output_schema": thaw_json(spec.output_schema),
            "routes": spec.routes,
            "timeout_seconds": spec.timeout_seconds,
            "max_attempts": spec.max_attempts,
        }
    return {
        "kind": "request_input",
        "name": spec.name,
        "action_code": spec.action_code,
    }


@dataclass(frozen=True, slots=True)
class AdkApplicationBuildContext:
    """SDK objects made available to a trusted application factory."""

    model: object
    tools: Mapping[str, object]
    function_nodes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        tool_names = tuple(self.tools)
        node_names = tuple(self.function_nodes)
        if any(_ACTION_CODE.fullmatch(name) is None for name in tool_names):
            raise ValueError("build context tool names must be stable identifiers")
        if any(_ACTION_CODE.fullmatch(name) is None for name in node_names):
            raise ValueError("build context node names must be stable identifiers")
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(
            self,
            "function_nodes",
            MappingProxyType(dict(self.function_nodes)),
        )


AdkApplicationFactory = Callable[[AdkApplicationBuildContext], object]


@dataclass(frozen=True, slots=True)
class AdkApplicationSpec:
    """One immutable, server-owned ADK application definition."""

    agent_id: str
    revision: str
    kind: ApplicationKind
    factory: AdkApplicationFactory
    tool_names: tuple[str, ...] = ()
    action_specs: tuple[WorkflowActionSpec, ...] = ()
    function_nodes: tuple[WorkflowFunctionNodeSpec, ...] = ()
    output_format: WorkflowOutputFormat = "text"
    resumable: bool = False
    fingerprint: str = field(init=False)
    _factory_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if _APPLICATION_ID.fullmatch(self.agent_id) is None:
            raise ValueError("agent_id must be a stable lowercase identifier")
        if _REVISION.fullmatch(self.revision) is None:
            raise ValueError("revision must be a stable ASCII identifier")
        if self.kind not in {"graph", "team"}:
            raise ValueError("application kind is invalid")
        if not callable(self.factory):
            raise ValueError("application factory must be callable")
        factory_fingerprint = _callable_semantic_digest(self.factory)
        object.__setattr__(self, "_factory_fingerprint", factory_fingerprint)
        function_nodes = tuple(self.function_nodes)
        object.__setattr__(self, "function_nodes", function_nodes)
        if len(set(self.tool_names)) != len(self.tool_names) or any(
            _ACTION_CODE.fullmatch(name) is None for name in self.tool_names
        ):
            raise ValueError("tool_names must contain unique stable identifiers")
        action_codes = [action.code for action in self.action_specs]
        if len(set(action_codes)) != len(action_codes):
            raise ValueError("action_specs must contain unique action codes")
        node_names = [node.name for node in function_nodes]
        if len(set(node_names)) != len(node_names):
            raise ValueError("function_nodes must contain unique node names")
        if self.kind != "graph" and function_nodes:
            raise ValueError("function_nodes are supported only for graph applications")
        action_code_set = set(action_codes)
        if any(
            isinstance(node, WorkflowRequestInputNodeSpec)
            and node.action_code not in action_code_set
            for node in function_nodes
        ):
            raise ValueError("request-input nodes must reference an action_spec")
        if self.resumable != bool(self.action_specs):
            raise ValueError("resumable applications must declare action_specs")
        if self.output_format not in {"text", "json"}:
            raise ValueError("output_format is invalid")
        material = json.dumps(
            {
                "agent_id": self.agent_id,
                "factory": factory_fingerprint,
                "kind": self.kind,
                "resumable": self.resumable,
                "revision": self.revision,
                "tool_names": self.tool_names,
                "action_specs": [
                    {
                        "code": action.code,
                        "kind": action.kind,
                        "response_schema": thaw_json(action.response_schema),
                    }
                    for action in self.action_specs
                ],
                "function_nodes": [
                    _workflow_function_node_material(node)
                    for node in self.function_nodes
                ],
                "output_format": self.output_format,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        object.__setattr__(self, "fingerprint", hashlib.sha256(material).hexdigest())


class AdkApplicationRegistry:
    """Frozen lookup for application-owned definitions."""

    def __init__(self, applications: Sequence[AdkApplicationSpec] = ()) -> None:
        entries: dict[str, AdkApplicationSpec] = {}
        for application in applications:
            if application.agent_id in entries:
                raise ValueError("agent_id must be unique")
            entries[application.agent_id] = application
        self._entries = MappingProxyType(entries)

    def resolve(self, agent_id: str) -> AdkApplicationSpec | None:
        return self._entries.get(agent_id)


@dataclass(frozen=True, slots=True)
class WorkflowPendingAction:
    """Allowlisted action metadata plus an internal-only runtime identifier."""

    runtime_action_id: str
    code: str
    kind: WorkflowActionKind

    def __post_init__(self) -> None:
        if not self.runtime_action_id or len(self.runtime_action_id) > 256:
            raise ValueError("runtime action identifier is invalid")
        if _ACTION_CODE.fullmatch(self.code) is None:
            raise ValueError("action code must be a stable lowercase identifier")
        if self.kind not in {"input", "confirmation"}:
            raise ValueError("action kind is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowActionResponse:
    runtime_action_id: str
    kind: WorkflowActionKind
    value: Any

    def __post_init__(self) -> None:
        if not self.runtime_action_id or len(self.runtime_action_id) > 256:
            raise ValueError("runtime action identifier is invalid")
        if self.kind == "input":
            object.__setattr__(self, "value", _freeze_strict_json(self.value))
        elif self.kind == "confirmation":
            if not isinstance(self.value, bool):
                raise ValueError("confirmation action response must be boolean")
        else:
            raise ValueError("action kind is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowInvocation:
    session_id: str
    text: str | None = None
    resume_token: str | None = None
    responses: tuple[WorkflowActionResponse, ...] = ()

    def __post_init__(self) -> None:
        initial = self.text is not None
        resumed = self.resume_token is not None
        if initial == resumed:
            raise ValueError("invocation must contain initial text or resume state")
        if initial and (not self.text or self.responses):
            raise ValueError("initial invocation payload is invalid")
        if resumed and (not self.resume_token or not self.responses):
            raise ValueError("resume invocation payload is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowInvocationResult:
    state: WorkflowResultState
    output: Any = None
    output_format: WorkflowOutputFormat = "text"
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str | None = None
    finish_reason: str | None = None
    event_count: int = 0
    actions: tuple[WorkflowPendingAction, ...] = ()
    resume_token: str | None = None

    def __post_init__(self) -> None:
        if self.event_count < 0:
            raise ValueError("event_count must not be negative")
        if self.state == "succeeded":
            if self.actions or self.resume_token is not None:
                raise ValueError("succeeded result must contain only final output")
            if self.output_format == "text":
                if not isinstance(self.output, str) or not self.output:
                    raise ValueError("text output must be a non-empty string")
            elif self.output_format == "json":
                object.__setattr__(self, "output", _freeze_strict_json(self.output))
            else:
                raise ValueError("output format is invalid")
            return
        if self.state != "requires_action":
            raise ValueError("workflow result state is invalid")
        if self.output is not None or not self.actions or not self.resume_token:
            raise ValueError("action result must contain actions and resume token")
        runtime_ids = [action.runtime_action_id for action in self.actions]
        if len(set(runtime_ids)) != len(runtime_ids):
            raise ValueError("pending runtime action identifiers must be unique")


class WorkflowRuntime(Protocol):
    provider_id: ProviderId

    async def invoke(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        identity: AgentIdentity,
        tools: Sequence[AgentToolSpec] = (),
        timeout_seconds: float | None = None,
    ) -> WorkflowInvocationResult: ...

    async def delete_session(
        self,
        application: AdkApplicationSpec,
        *,
        identity: AgentIdentity,
        session_id: str,
    ) -> None: ...
