import asyncio
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from litellm.types.utils import Choices, Message, ModelResponse, ModelResponseStream

_GITHUB_TOKEN_PREFIXES: Tuple[str, ...] = (
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
)
_SDK_DISABLE_ENV_VAR = "LITELLM_DISABLE_GITHUB_COPILOT_SDK"
_SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
_SUPPORTED_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}
_STREAM_SENTINEL = object()


class GithubCopilotSDKFallbackError(Exception):
    """Raised when LiteLLM should fall back to the legacy Copilot transport."""


@dataclass(frozen=True)
class GithubCopilotSDKChatRequest:
    model: str
    messages: List[Dict[str, Any]]
    acompletion: bool
    stream: bool
    timeout: Optional[Any]
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    base_url: Optional[str] = None
    extra_headers: Optional[Dict[str, Any]] = None
    functions: Optional[Any] = None
    function_call: Optional[Any] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stream_options: Optional[Any] = None
    stop: Optional[Any] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    modalities: Optional[Any] = None
    prediction: Optional[Any] = None
    audio: Optional[Any] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, int]] = None
    user: Optional[str] = None
    response_format: Optional[Any] = None
    seed: Optional[int] = None
    tools: Optional[Any] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    reasoning_effort: Optional[str] = None
    thinking: Optional[Any] = None
    web_search_options: Optional[Any] = None


def should_use_github_copilot_sdk_chat(
    request: GithubCopilotSDKChatRequest,
) -> bool:
    if _env_flag_enabled(_SDK_DISABLE_ENV_VAR):
        return False

    if request.api_base is not None or request.base_url is not None:
        return False

    if request.extra_headers:
        return False

    unsupported_request_fields = (
        request.functions,
        request.function_call,
        request.temperature,
        request.top_p,
        request.n,
        request.stream_options,
        request.stop,
        request.max_tokens,
        request.max_completion_tokens,
        request.modalities,
        request.prediction,
        request.audio,
        request.presence_penalty,
        request.frequency_penalty,
        request.logit_bias,
        request.response_format,
        request.seed,
        request.tools,
        request.tool_choice,
        request.parallel_tool_calls,
        request.logprobs,
        request.top_logprobs,
        request.thinking,
        request.web_search_options,
    )
    if any(value is not None for value in unsupported_request_fields):
        return False

    if request.reasoning_effort not in (None, *tuple(_SUPPORTED_REASONING_EFFORTS)):
        return False

    if not request.messages or _message_list_has_unsupported_content(request.messages):
        return False

    return _get_last_non_system_role(request.messages) in {"user", "tool"}


def github_copilot_sdk_chat_completion(
    request: GithubCopilotSDKChatRequest,
):
    if request.acompletion:
        return _github_copilot_sdk_chat_completion_async(request)
    if _has_running_loop():
        raise GithubCopilotSDKFallbackError(
            "github-copilot-sdk sync completion cannot be used from within an "
            "existing event loop"
        )
    return asyncio.run(_github_copilot_sdk_chat_completion_async(request))


async def _github_copilot_sdk_chat_completion_async(
    request: GithubCopilotSDKChatRequest,
):
    system_message, prompt = _build_sdk_prompt(request.messages)
    timeout_seconds = _normalize_timeout_seconds(request.timeout)

    try:
        sdk = _import_sdk()
    except ModuleNotFoundError as exc:
        raise GithubCopilotSDKFallbackError(
            "github-copilot-sdk is not installed"
        ) from exc

    try:
        client, session = await _create_sdk_session(
            sdk=sdk,
            model=_strip_provider_prefix(request.model),
            system_message=system_message,
            stream=request.stream,
            api_key=request.api_key,
            reasoning_effort=request.reasoning_effort,
        )
    except Exception as exc:
        raise GithubCopilotSDKFallbackError(
            f"Unable to initialize github-copilot-sdk session: {exc}"
        ) from exc

    if request.stream:
        return _GithubCopilotSDKStream(
            client=client,
            session=session,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            model=request.model,
            async_mode=request.acompletion,
            sdk=sdk,
        )

    try:
        event = await session.send_and_wait(prompt, timeout=timeout_seconds)
        content = ""
        if event is not None and getattr(event, "data", None) is not None:
            content = getattr(event.data, "content", "") or ""
        return _build_model_response(model=request.model, content=content)
    finally:
        await session.disconnect()
        await client.stop()


async def _create_sdk_session(
    *,
    sdk: Tuple[Any, Any, Any, Any],
    model: str,
    system_message: Optional[str],
    stream: bool,
    api_key: Optional[str],
    reasoning_effort: Optional[str],
):
    CopilotClient, PermissionHandler, SubprocessConfig, _SessionEventType = sdk
    session_kwargs: Dict[str, Any] = {
        "on_permission_request": PermissionHandler.approve_all,
        "model": model,
        "streaming": stream,
        "available_tools": [],
        "working_directory": os.getcwd(),
    }
    if system_message:
        session_kwargs["system_message"] = {"content": system_message}
    if reasoning_effort in _SUPPORTED_REASONING_EFFORTS:
        session_kwargs["reasoning_effort"] = reasoning_effort

    github_token = _maybe_resolve_github_token(api_key)
    config = None
    if github_token is not None:
        config = SubprocessConfig(
            github_token=github_token,
            use_logged_in_user=False,
        )

    client = CopilotClient(config)
    session = await client.create_session(**session_kwargs)
    return client, session


def _build_sdk_prompt(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    system_segments: List[str] = []
    prompt_segments: List[str] = []

    for message in messages:
        content = _extract_message_text(message)
        if content is None:
            raise GithubCopilotSDKFallbackError(
                "Unsupported GitHub Copilot SDK message content"
            )

        role = message.get("role")
        if role == "system":
            if content:
                system_segments.append(content)
            continue

        prompt_segments.append(f"{_format_sdk_role(role, message)}:\n{content}")

    if not prompt_segments:
        raise GithubCopilotSDKFallbackError(
            "GitHub Copilot SDK chat path requires at least one non-system message"
        )

    prompt_segments.append("Assistant:")
    return "\n\n".join(system_segments).strip() or None, "\n\n".join(prompt_segments)


def _extract_message_text(message: Dict[str, Any]) -> Optional[str]:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    text_segments: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type not in (None, "text", "input_text"):
            return None
        text_value = item.get("text", item.get("content"))
        if not isinstance(text_value, str):
            return None
        text_segments.append(text_value)
    return "\n".join(segment for segment in text_segments if segment)


def _message_list_has_unsupported_content(messages: List[Dict[str, Any]]) -> bool:
    return any(_message_is_unsupported(message) for message in messages)


def _message_is_unsupported(message: Dict[str, Any]) -> bool:
    role = message.get("role")
    if role not in _SUPPORTED_MESSAGE_ROLES:
        return True

    if _extract_message_text(message) is None:
        return True

    if message.get("tool_calls") is not None:
        return True

    if message.get("function_call") is not None:
        return True

    if message.get("name") is not None:
        return True

    allowed_keys = {"role", "content"}
    if role == "tool":
        allowed_keys.add("tool_call_id")

    return any(key not in allowed_keys for key in message)


def _get_last_non_system_role(messages: List[Dict[str, Any]]) -> Optional[str]:
    for message in reversed(messages):
        role = message.get("role")
        if role != "system":
            return role
    return None


def _format_sdk_role(role: Optional[str], message: Dict[str, Any]) -> str:
    if role == "assistant":
        return "Assistant"
    if role == "tool":
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return f"Tool ({tool_call_id})"
        return "Tool"
    return "User"


def _strip_provider_prefix(model: str) -> str:
    if model.startswith("github_copilot/"):
        return model.split("/", 1)[1]
    return model


def _normalize_timeout_seconds(timeout: Optional[Any]) -> float:
    if timeout is None:
        return 60.0

    read_timeout = getattr(timeout, "read", None)
    if isinstance(read_timeout, (int, float)) and read_timeout > 0:
        return float(read_timeout)

    if isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)

    return 60.0


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _build_model_response(model: str, content: str) -> ModelResponse:
    return ModelResponse(
        model=model,
        choices=[
            Choices(
                index=0,
                finish_reason="stop",
                message=Message(role="assistant", content=content),
            )
        ],
    )


def _build_stream_chunk(
    *,
    model: str,
    response_id: str,
    created: int,
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    include_role: bool = False,
) -> ModelResponseStream:
    delta: Dict[str, Any] = {}
    if include_role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content

    return ModelResponseStream(
        id=response_id,
        created=created,
        model=model,
        choices=[
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    )


def _maybe_resolve_github_token(api_key: Optional[str]) -> Optional[str]:
    if api_key is not None and api_key.startswith(_GITHUB_TOKEN_PREFIXES):
        return api_key
    return None


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _import_sdk() -> Tuple[Any, Any, Any, Any]:
    from copilot import CopilotClient, SubprocessConfig
    from copilot.generated.session_events import SessionEventType
    from copilot.session import PermissionHandler

    return CopilotClient, PermissionHandler, SubprocessConfig, SessionEventType


class _GithubCopilotSDKStream:
    def __init__(
        self,
        *,
        client: Any,
        session: Any,
        prompt: str,
        timeout_seconds: float,
        model: str,
        async_mode: bool,
        sdk: Tuple[Any, Any, Any, Any],
    ) -> None:
        self._client = client
        self._session = session
        self._prompt = prompt
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._async_mode = async_mode
        self._sdk = sdk
        self._response_id = f"chatcmpl-copilot-sdk-{int(time.time() * 1000)}"
        self._created = int(time.time())

    def __iter__(self):
        if self._async_mode:
            raise RuntimeError("Cannot sync-iterate an async GitHub Copilot SDK stream")
        return _SyncStreamBridge(self._iterate_async)

    def __aiter__(self):
        return self._iterate_async()

    async def _iterate_async(self) -> AsyncIterator[ModelResponseStream]:
        _CopilotClient, _PermissionHandler, _SubprocessConfig, SessionEventType = self._sdk
        stream_queue: asyncio.Queue[Any] = asyncio.Queue()
        role_emitted = False

        def on_event(event: Any) -> None:
            nonlocal role_emitted
            if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                delta_content = getattr(event.data, "delta_content", None)
                if delta_content:
                    stream_queue.put_nowait(
                        _build_stream_chunk(
                            model=self._model,
                            response_id=self._response_id,
                            created=self._created,
                            content=delta_content,
                            include_role=not role_emitted,
                        )
                    )
                    role_emitted = True
            elif event.type == SessionEventType.SESSION_IDLE:
                stream_queue.put_nowait(
                    _build_stream_chunk(
                        model=self._model,
                        response_id=self._response_id,
                        created=self._created,
                        finish_reason="stop",
                    )
                )
                stream_queue.put_nowait(_STREAM_SENTINEL)

        unsubscribe = self._session.on(on_event)
        send_task = asyncio.create_task(
            self._session.send_and_wait(
                self._prompt,
                timeout=self._timeout_seconds,
            )
        )
        try:
            while True:
                item = await stream_queue.get()
                if item is _STREAM_SENTINEL:
                    break
                yield item
            await send_task
        finally:
            unsubscribe()
            await self._session.disconnect()
            await self._client.stop()


class _SyncStreamBridge:
    def __init__(
        self,
        async_iter_factory: Callable[[], AsyncIterator[ModelResponseStream]],
    ) -> None:
        self._async_iter_factory = async_iter_factory
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        async def consume() -> None:
            try:
                async for item in self._async_iter_factory():
                    self._queue.put(item)
            except Exception as exc:  # pragma: no cover - raised in __next__
                self._queue.put(exc)
            finally:
                self._queue.put(_STREAM_SENTINEL)

        asyncio.run(consume())

    def __iter__(self):
        return self

    def __next__(self) -> ModelResponseStream:
        item = self._queue.get()
        if item is _STREAM_SENTINEL:
            raise StopIteration
        if isinstance(item, Exception):
            raise item
        return item
