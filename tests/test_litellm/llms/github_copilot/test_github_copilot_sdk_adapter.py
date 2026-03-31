from unittest.mock import MagicMock, patch

import pytest

from litellm import Message
from litellm.llms.github_copilot.sdk_adapter import (
    GithubCopilotSDKChatRequest,
    GithubCopilotSDKFallbackError,
    _GithubCopilotSDKStream,
    github_copilot_sdk_chat_completion,
    should_use_github_copilot_sdk_chat,
)
from litellm.main import completion as main_completion
from litellm.types.utils import Choices, ModelResponse


def _build_response(content: str) -> ModelResponse:
    return ModelResponse(
        model="github_copilot/gpt-4",
        choices=[
            Choices(
                index=0,
                finish_reason="stop",
                message=Message(role="assistant", content=content),
            )
        ],
    )


def test_should_use_github_copilot_sdk_chat_for_simple_text_messages():
    request = GithubCopilotSDKChatRequest(
        model="github_copilot/gpt-4",
        messages=[{"role": "user", "content": "Hello from LiteLLM"}],
        acompletion=False,
        stream=False,
        timeout=30,
    )

    assert should_use_github_copilot_sdk_chat(request) is True


def test_should_not_use_github_copilot_sdk_chat_for_vision_messages():
    request = GithubCopilotSDKChatRequest(
        model="github_copilot/gpt-4",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.png"},
                    },
                ],
            }
        ],
        acompletion=False,
        stream=False,
        timeout=30,
    )

    assert should_use_github_copilot_sdk_chat(request) is False


@pytest.mark.parametrize(
    "message",
    [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {
            "role": "developer",
            "content": "Use terse answers",
        },
    ],
)
def test_should_not_use_github_copilot_sdk_chat_for_unsupported_message_semantics(
    message,
):
    request = GithubCopilotSDKChatRequest(
        model="github_copilot/gpt-4",
        messages=[message, {"role": "user", "content": "Hello from LiteLLM"}],
        acompletion=False,
        stream=False,
        timeout=30,
    )

    assert should_use_github_copilot_sdk_chat(request) is False


@pytest.mark.asyncio
async def test_sync_sdk_completion_falls_back_when_event_loop_is_running():
    request = GithubCopilotSDKChatRequest(
        model="github_copilot/gpt-4",
        messages=[{"role": "user", "content": "Hello from LiteLLM"}],
        acompletion=False,
        stream=False,
        timeout=30,
    )

    with pytest.raises(GithubCopilotSDKFallbackError):
        github_copilot_sdk_chat_completion(request)


class _FakeSessionEventType:
    ASSISTANT_MESSAGE_DELTA = "assistant_message_delta"
    SESSION_IDLE = "session_idle"


class _FakeEvent:
    def __init__(self, event_type, data=None):
        self.type = event_type
        self.data = data


class _FakeEventData:
    def __init__(self, *, delta_content=None):
        self.delta_content = delta_content


class _FakeSession:
    def __init__(self):
        self._callbacks = []
        self.disconnected = False

    def on(self, callback):
        self._callbacks.append(callback)

        def unsubscribe():
            self._callbacks.remove(callback)

        return unsubscribe

    async def send_and_wait(self, prompt, timeout):
        assert prompt == "User:\nHello from LiteLLM\n\nAssistant:"
        assert timeout == 30.0
        for callback in list(self._callbacks):
            callback(
                _FakeEvent(
                    _FakeSessionEventType.ASSISTANT_MESSAGE_DELTA,
                    _FakeEventData(delta_content="Hello"),
                )
            )
            callback(
                _FakeEvent(
                    _FakeSessionEventType.ASSISTANT_MESSAGE_DELTA,
                    _FakeEventData(delta_content=" from stream"),
                )
            )
            callback(_FakeEvent(_FakeSessionEventType.SESSION_IDLE))
        return None

    async def disconnect(self):
        self.disconnected = True


class _FakeClient:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def test_github_copilot_sdk_stream_emits_chunks_and_stop_signal():
    client = _FakeClient()
    session = _FakeSession()
    stream = _GithubCopilotSDKStream(
        client=client,
        session=session,
        prompt="User:\nHello from LiteLLM\n\nAssistant:",
        timeout_seconds=30.0,
        model="github_copilot/gpt-4",
        async_mode=False,
        sdk=(None, None, None, _FakeSessionEventType),
    )

    chunks = list(stream)

    assert [chunk.choices[0].delta.content for chunk in chunks[:-1]] == [
        "Hello",
        " from stream",
    ]
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert session.disconnected is True
    assert client.stopped is True


@patch("litellm.main.github_copilot_sdk_chat_completion")
@patch("litellm.main.should_use_github_copilot_sdk_chat")
@patch("litellm.llms.github_copilot.authenticator.Authenticator.get_api_key")
@patch("litellm.llms.openai.openai.OpenAIChatCompletion.completion")
def test_completion_prefers_github_copilot_sdk_for_supported_requests(
    mock_openai_completion,
    mock_get_api_key,
    mock_should_use_sdk,
    mock_sdk_completion,
):
    mock_get_api_key.return_value = "gh.test-key-123456789"
    mock_should_use_sdk.return_value = True
    mock_sdk_completion.return_value = _build_response("Hello from the SDK path")
    litellm_logging_obj = MagicMock()

    response = main_completion.__wrapped__(
        model="github_copilot/gpt-4",
        messages=[{"role": "user", "content": "Who are you?"}],
        api_key="gh.test-key-123456789",
        litellm_logging_obj=litellm_logging_obj,
    )

    assert response.choices[0].message.content == "Hello from the SDK path"
    mock_sdk_completion.assert_called_once()
    mock_openai_completion.assert_not_called()


@patch("litellm.llms.github_copilot.authenticator.Authenticator.get_api_key")
@patch("litellm.llms.openai.openai.OpenAIChatCompletion.completion")
@patch("litellm.main.github_copilot_sdk_chat_completion")
@patch("litellm.main.should_use_github_copilot_sdk_chat")
def test_completion_falls_back_to_legacy_transport_when_sdk_declines(
    mock_should_use_sdk,
    mock_sdk_completion,
    mock_openai_completion,
    mock_get_api_key,
):
    from litellm.llms.github_copilot.sdk_adapter import GithubCopilotSDKFallbackError

    mock_should_use_sdk.return_value = True
    mock_sdk_completion.side_effect = GithubCopilotSDKFallbackError("sdk unavailable")
    mock_get_api_key.return_value = "gh.test-key-123456789"

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Hello from the legacy path"
    mock_openai_completion.return_value = mock_response
    litellm_logging_obj = MagicMock()

    response = main_completion.__wrapped__(
        model="github_copilot/gpt-4",
        messages=[{"role": "user", "content": "Who are you?"}],
        api_key="gh.test-key-123456789",
        litellm_logging_obj=litellm_logging_obj,
    )

    assert response.choices[0].message.content == "Hello from the legacy path"
    mock_openai_completion.assert_called_once()
