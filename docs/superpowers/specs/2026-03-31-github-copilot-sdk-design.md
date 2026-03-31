# GitHub Copilot SDK integration design

## Problem

LiteLLM already exposes a `github_copilot/` provider, but it currently authenticates and talks to Copilot through a custom OAuth-device-flow and HTTP implementation. That works, but it diverges from GitHub's official `github-copilot-sdk`, which is the supported path for Copilot CLI-backed application integrations.

The challenge is that the SDK is built around a Copilot CLI session runtime, while LiteLLM exposes OpenAI-style request/response APIs. A naive full replacement would risk breaking existing streaming, embeddings, reasoning, and compatibility behaviors.

## Scope

This design changes only the `github_copilot/` provider.

The plain `github/` provider remains unchanged because it targets GitHub Models' OpenAI-compatible HTTP endpoint, not the Copilot CLI runtime.

## Options considered

### Option 1: Full immediate replacement

Replace all `github_copilot/` chat, responses, and embedding internals with the SDK immediately.

Pros:
- maximally aligned with GitHub's official integration path
- removes most custom Copilot transport/auth code quickly

Cons:
- high migration risk because the SDK is session-oriented, not request-oriented
- embeddings do not clearly map to the SDK surface
- Responses API semantics are richer than a simple session send/wait flow
- likely to regress existing LiteLLM compatibility

### Option 2: Hybrid adapter with automatic safe fallback

Add an SDK-backed adapter for the request shapes that map cleanly, use it automatically when possible, and fall back to the current implementation when the SDK is unavailable or the request uses unsupported features.

Pros:
- gets LiteLLM onto the official SDK path for the most natural Copilot use case
- preserves existing behavior for unsupported cases
- allows incremental migration without a large compatibility cliff

Cons:
- temporary dual-path maintenance
- not every `github_copilot/` endpoint will use the SDK on day one

### Option 3: Add a separate provider route

Introduce a new provider name for SDK-backed Copilot and keep the current provider unchanged.

Pros:
- lowest migration risk
- easy A/B testing

Cons:
- fragments the user-facing provider surface
- keeps the old path as the default, which does not actually solve the integration goal

## Recommendation

Use **Option 2**.

It gives LiteLLM a real `github-copilot-sdk` integration now, while keeping the existing provider stable. It also respects the SDK's current shape instead of forcing raw Responses/embedding behavior through an agent-session API that does not match cleanly.

## Architecture

### 1. New internal SDK adapter

Add a new internal module under `litellm/llms/github_copilot/` that:

- imports `github-copilot-sdk` lazily
- creates and manages a `CopilotClient`
- creates a short-lived session per LiteLLM request
- converts LiteLLM chat messages into:
  - SDK `system_message` content
  - a single prompt transcript for the session send call
- converts SDK assistant events back into LiteLLM `ModelResponse` / streaming chunks

### 2. Capability guardrail

The adapter should only claim requests it can faithfully support in the first pass:

- text-only chat completions
- normal streaming and non-streaming assistant text output

The adapter should decline and trigger fallback for:

- vision/image content
- embedding calls
- raw Responses API calls
- request shapes that depend on OpenAI tool-calling semantics the SDK cannot faithfully reproduce yet

### 3. Runtime selection

For `github_copilot/` chat completions:

- try SDK path first when:
  - SDK package import succeeds
  - request shape is supported
- otherwise fall back to the current legacy OpenAI-compatible HTTP path

Fallback is also used if:

- the Copilot CLI cannot be started
- the SDK raises an auth or runtime startup error before a session successfully completes

### 4. Provider boundary

This change is intentionally **not** applied to `github/`.

Reason:
- `github/` maps to GitHub Models' OpenAI-compatible API
- `github-copilot-sdk` is specifically for Copilot CLI-backed agent sessions
- conflating them would make both integrations less correct

## Data flow

### Chat completion, non-streaming

1. LiteLLM receives `completion(model="github_copilot/...")`
2. Request passes through existing provider resolution
3. New SDK guard checks if request is eligible
4. Adapter creates `CopilotClient` + session
5. Adapter builds:
   - combined system message from LiteLLM system messages
   - prompt transcript from non-system conversation turns
6. Adapter sends prompt with `send_and_wait()`
7. Final assistant message is converted into LiteLLM `ModelResponse`

### Chat completion, streaming

1. Same request selection as above
2. Session created with `streaming=True`
3. Adapter listens to `assistant.message_delta`, `assistant.message`, and `session.idle`
4. Deltas are emitted as LiteLLM streaming chunks
5. Final assistant message / stop chunk closes the iterator

## Error handling

- Missing SDK package: log/debug and fall back
- Copilot CLI startup failure: log/debug and fall back
- Unsupported request shape: fall back without partial execution
- SDK runtime failure after the request has already been claimed: raise a LiteLLM provider error rather than silently returning a legacy result

The key rule is to avoid silent behavior changes mid-request while still making startup/availability failures non-breaking.

## Testing

Add targeted tests for:

- SDK path used for simple `github_copilot/` chat completion
- fallback to legacy path when SDK unavailable
- fallback to legacy path for unsupported request shapes
- prompt/system-message conversion
- streaming delta conversion

## Documentation

Update `docs/my-website/docs/providers/github_copilot.md` to explain:

- LiteLLM now supports an SDK-backed Copilot chat path
- the SDK path requires the Copilot CLI environment
- unsupported cases still use the legacy transport path

## Deferred work

These are explicitly out of scope for this pass:

- migrating embeddings onto the SDK
- replacing the raw Responses API path with the SDK
- removing the legacy authenticator immediately

Those should be handled in later follow-ups once the SDK/CLI mapping is proven for the chat surface.
