# GitHub Copilot SDK model discovery design

## Problem

LiteLLM now has an SDK-backed execution path for eligible `github_copilot/` chat requests, but model discovery is still incomplete.

Today, the proxy model-list surfaces and the dashboard add-model flow do not ask the GitHub Copilot SDK which models are actually available for the authenticated Copilot runtime. As a result:

- `/v1/models` and related proxy model-list responses do not reflect runtime Copilot availability
- the dashboard provider picker relies on static provider metadata instead of live Copilot discovery
- users can configure `github_copilot/...` models manually, but the UI cannot reliably resolve or suggest the real model list

The goal of this follow-up is to make GitHub Copilot a full integration for model discovery, while preserving safe fallback behavior when the Copilot CLI or token is not ready.

## Scope

This design covers only model discovery for the `github_copilot/` provider.

It does not change:

- the plain `github/` provider
- the already-shipped SDK-backed chat execution path
- embeddings / responses migration onto the SDK

## Goals

1. Expose GitHub Copilot SDK-discovered models through shared backend logic.
2. Make the dashboard add-model flow use live Copilot provider models when available.
3. Reuse the same discovery source for proxy model-list surfaces instead of introducing one-off UI logic.
4. Degrade safely when discovery is unavailable, without inventing fake model availability.

## Options considered

### Option 1: UI-only provider fetch

Add a new frontend-only call for `github_copilot` model discovery and fill the add-model dropdown from that call.

Pros:

- smallest implementation
- directly fixes the add-model UX

Cons:

- `/v1/models` and other backend consumers remain unaware of Copilot discovery
- duplicates provider logic across backend and frontend
- not a full integration

### Option 2: Shared backend discovery service with UI consumption

Add a backend discovery service that queries `github-copilot-sdk`, normalizes the result, and is reused by both proxy model-list surfaces and the dashboard provider-model flow.

Pros:

- one source of truth
- fixes both API and UI behavior
- easiest to extend later for other dynamic providers

Cons:

- touches both backend and UI
- needs caching and failure semantics

### Option 3: Static Copilot model registry

Ship a curated static list of Copilot models and optionally refresh it later.

Pros:

- very low runtime complexity
- no discovery/auth dependency at request time

Cons:

- stale by design
- does not reflect the authenticated runtime
- defeats the value of SDK-backed discovery

## Recommendation

Use **Option 2**.

This keeps discovery authoritative and backend-owned while still allowing the UI to benefit from it. It also matches the existing LiteLLM proxy shape better than embedding Copilot-specific rules into frontend helpers.

## Architecture

### 1. New backend discovery helper

Add a new helper module under `litellm/llms/github_copilot/` or an adjacent proxy-facing helper that:

- imports `github-copilot-sdk`
- creates a `CopilotClient`
- calls `client.list_models()`
- normalizes SDK model metadata into a LiteLLM-friendly structure

The helper should return a structured result such as:

- normalized model IDs, using the existing `github_copilot/<model-id>` naming convention for proxy-facing lists
- display metadata from the SDK when available
- capability metadata that is safe to surface, such as reasoning support when present

This helper is read-only and separate from the request execution adapter, but it should reuse the same auth/runtime assumptions as the SDK chat path.

### 2. Proxy-level discovery service with caching

Wrap the raw SDK discovery helper in a proxy-facing service that:

- caches successful discovery results for a short TTL
- optionally caches recent failures for a shorter TTL to avoid repeated CLI spawn storms
- returns structured status information the caller can use for graceful degradation

Recommended behavior:

- success TTL: a few minutes
- failure TTL: shorter than success TTL

The service should not silently fabricate Copilot models if discovery fails. Instead, it should distinguish between:

- discovery succeeded with models
- discovery unavailable because auth/CLI/runtime is not ready
- discovery failed unexpectedly

### 3. Shared use in proxy model-list endpoints

Integrate Copilot discovery into the existing proxy model-list assembly path instead of adding a parallel endpoint-only implementation.

The proxy should:

- continue returning configured proxy models as it does today
- augment the available-model list with discovered `github_copilot/...` models when discovery is available
- avoid exposing duplicate model names if a discovered model is already represented in the assembled list

This change should be routed through existing shared helpers such as `get_available_models_for_user()` so `/models`, `/v1/models`, and related info endpoints stay consistent.

### 4. Dedicated provider-model endpoint for the dashboard

The dashboard add-model form currently builds provider model choices from static provider metadata. For Copilot, that is not enough.

Add a backend endpoint for provider-scoped dynamic model discovery, for example a route that accepts a provider name and returns:

- `models`
- `source` (`dynamic` or `static`)
- optional `warning` / `status`

For the first pass, only `github_copilot` needs dynamic behavior. Other providers can keep using static frontend-derived lists until there is a reason to migrate them.

### 5. UI fallback behavior

Update the dashboard add-model flow so that when the user selects `Github Copilot`:

- the UI first requests dynamic provider models from the backend
- on success, the dropdown uses the backend-discovered list
- on controlled unavailability, the UI falls back to the existing static helper and shows a warning message

This preserves usability:

- the form remains functional even before Copilot auth is configured
- users see the live list when runtime discovery is available
- the UI does not need Copilot-specific SDK logic

## Data flow

### Proxy model list

1. Caller hits `/models` or `/v1/models`.
2. Existing model-list path builds the user-visible available model set.
3. Shared availability helper asks the Copilot discovery service for `github_copilot` models.
4. On success, normalized models are merged into the returned list.
5. On controlled discovery unavailability, the proxy returns the normal list without fake Copilot entries.

### Dashboard add-model flow

1. User selects `Github Copilot` in the add-model form.
2. Frontend calls the new provider-model discovery endpoint.
3. Backend returns dynamic Copilot models when available.
4. UI populates the model-name suggestions from that response.
5. If discovery is unavailable, UI falls back to the current static helper and shows the backend message.

## Error handling

The design should preserve explicit, behavior-safe failures.

Backend rules:

- missing SDK dependency: return controlled unavailable status
- Copilot CLI not installed: return controlled unavailable status
- missing/invalid auth token: return controlled unavailable status
- unexpected SDK/runtime exception: surface a provider discovery error to logs and return safe unavailability to the UI-facing endpoint

Proxy model-list rule:

- never claim Copilot models were discovered unless discovery actually succeeded

UI rules:

- if dynamic discovery succeeds, use it
- if dynamic discovery is unavailable, fall back to existing static provider models and show a non-blocking warning
- if the request fails unexpectedly, show a backend-derived error and keep the form usable

## Testing

### Backend tests

Add focused tests for:

- SDK model normalization
- caching success and failure behavior
- merge behavior in available model lists
- duplicate suppression when a discovered model is already present
- controlled unavailability for missing CLI/token/SDK

### Proxy tests

Add tests covering:

- `/models` and `/v1/models` include discovered Copilot models when discovery succeeds
- model-list responses remain stable when discovery is unavailable
- provider-model endpoint returns the expected payload shape

### UI tests

Add tests covering:

- selecting `Github Copilot` triggers backend provider-model fetch
- successful dynamic response populates the dropdown
- unavailable dynamic response falls back to static provider models
- warning/error messaging is rendered without breaking the form

## Documentation

Update the following documentation:

- `docs/my-website/docs/providers/github_copilot.md`
- the PR notes describing runtime setup and usage

Document that:

- live Copilot model discovery depends on the Copilot CLI runtime plus valid auth
- the dashboard will prefer discovered models for `github_copilot`
- proxy model-list surfaces only include discovered Copilot models when runtime discovery succeeds

## Rollout notes

- This feature should be additive and safe by default.
- The existing static provider model helper remains in place as the UI fallback.
- The existing SDK chat integration remains unchanged.
- Docker/Podman users still need the Copilot CLI available in the image and a valid `COPILOT_GITHUB_TOKEN`.

## Success criteria

The feature is complete when:

1. The backend can query GitHub Copilot SDK models and normalize them.
2. `/models` and `/v1/models` can reflect discovered Copilot models through shared logic.
3. The dashboard add-model flow can resolve Copilot model choices from the backend.
4. Failure paths degrade cleanly without misleading model availability.
