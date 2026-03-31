from unittest.mock import MagicMock, patch

from litellm import Message
from litellm.llms.github_copilot.sdk_adapter import (
    GithubCopilotSDKChatRequest,
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
