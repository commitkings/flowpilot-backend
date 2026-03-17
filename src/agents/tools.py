import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)


class ToolParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True)
class ToolParam:
    name: str
    param_type: ToolParamType
    description: str
    required: bool = True
    default: Any = None
    enum: list[str] | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: list[ToolParam] = field(default_factory=list)
    execute: Callable[..., Awaitable[Any]] = field(repr=False, default=None)

    def to_llm_schema(self) -> dict:
        properties = {}
        required_params = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.param_type.value,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop

            if param.required:
                required_params.append(param.name)

        schema: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                },
            },
        }
        if required_params:
            schema["function"]["parameters"]["required"] = required_params

        return schema


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: int = 0

    def to_message_content(self) -> str:
        if not self.success:
            return json.dumps({"error": self.error, "tool": self.tool_name})
        if isinstance(self.data, str):
            return self.data
        return json.dumps(self.data, default=str)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.call_log: list[dict] = []

    def register(self, tool: Tool) -> None:
        if tool.execute is None:
            raise ValueError(f"Tool '{tool.name}' has no execute function")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_llm_tools(self) -> list[dict]:
        return [tool.to_llm_schema() for tool in self._tools.values()]

    def to_tool_descriptions(self) -> str:
        lines = []
        for tool in self._tools.values():
            param_parts = []
            for p in tool.parameters:
                req = "required" if p.required else "optional"
                param_parts.append(
                    f"    - {p.name} ({p.param_type.value}, {req}): {p.description}"
                )
            params_str = (
                "\n".join(param_parts) if param_parts else "    (no parameters)"
            )
            lines.append(
                f"- **{tool.name}**: {tool.description}\n  Parameters:\n{params_str}"
            )
        return "\n\n".join(lines)

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.tool_name)
        if tool is None:
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=f"Unknown tool: {call.tool_name}. Available tools: {', '.join(self.tool_names)}",
            )

        expected_params = {p.name for p in tool.parameters}
        provided_params = set(call.arguments.keys())
        unknown_params = provided_params - expected_params
        if unknown_params:
            logger.warning(
                f"Tool '{call.tool_name}' received unknown params: {unknown_params}"
            )

        required_params = {p.name for p in tool.parameters if p.required}
        missing_params = required_params - provided_params
        if missing_params:
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=f"Missing required parameters: {', '.join(sorted(missing_params))}",
            )

        resolved_args = {}
        for param in tool.parameters:
            if param.name in call.arguments:
                resolved_args[param.name] = call.arguments[param.name]
            elif not param.required and param.default is not None:
                resolved_args[param.name] = param.default

        t0 = time.monotonic()
        try:
            result_data = await tool.execute(**resolved_args)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.info(f"Tool '{call.tool_name}' executed in {elapsed_ms}ms")
            self.call_log.append(
                {
                    "tool": call.tool_name,
                    "arguments": call.arguments,
                    "success": True,
                    "duration_ms": elapsed_ms,
                }
            )
            return ToolResult(
                tool_name=call.tool_name,
                success=True,
                data=result_data,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                f"Tool '{call.tool_name}' failed after {elapsed_ms}ms: {e}",
                exc_info=True,
            )
            self.call_log.append(
                {
                    "tool": call.tool_name,
                    "arguments": call.arguments,
                    "success": False,
                    "error": str(e),
                    "duration_ms": elapsed_ms,
                }
            )
            return ToolResult(
                tool_name=call.tool_name,
                success=True,
                data=result_data,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                f"Tool '{call.tool_name}' failed after {elapsed_ms}ms: {e}",
                exc_info=True,
            )
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=str(e),
                duration_ms=elapsed_ms,
            )


def parse_tool_calls_from_response(response_message) -> list[ToolCall]:
    if not hasattr(response_message, "tool_calls") or not response_message.tool_calls:
        return []

    calls = []
    for tc in response_message.tool_calls:
        try:
            arguments = (
                json.loads(tc.function.arguments)
                if isinstance(tc.function.arguments, str)
                else tc.function.arguments
            )
        except (json.JSONDecodeError, AttributeError):
            arguments = {}
            logger.warning(
                f"Failed to parse arguments for tool call: {tc.function.name}"
            )

        calls.append(
            ToolCall(
                tool_name=tc.function.name,
                arguments=arguments or {},
                call_id=tc.id or "",
            )
        )
    return calls
