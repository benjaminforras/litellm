# GitHub Copilot environment-token auth fallback design

## Problem

LiteLLM's `github_copilot` support now has an SDK-backed chat path, but the legacy GitHub Copilot transport still relies on `Authenticator()` and device-code login when no cached Copilot token files exist.

In proxy deployments, users often provide a proxy-scoped GitHub token through container environment variables such as `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. Today, the legacy path does not reliably consume those values before attempting device flow, which causes interactive login prompts even though a usable token is already available.

## Options considered

### Option 1: Fix `Authenticator` token precedence

Teach `Authenticator.get_access_token()` to use proxy-scoped environment tokens before reading cached files or initiating device-code flow.

Pros:
- smallest change
- fixes chat fallback, embeddings, responses, and any other legacy path using `Authenticator`
- aligns with current container documentation

Cons:
- still leaves the old transport in place for unsupported SDK cases

### Option 2: Patch only the SDK path

Improve only the SDK chat flow to read environment tokens.

Pros:
- useful for SDK-backed chat

Cons:
- does not fix the current interactive device-flow problem in legacy fallback paths
- incomplete for embeddings / responses

### Option 3: Remove device flow entirely

Require environment tokens and fail if missing.

Pros:
- simple operational model for server deployments

Cons:
- breaking change for local users who rely on device flow

## Recommendation

Use **Option 1**.

Keep device flow as the last-resort interactive path, but make environment-token auth the first non-interactive source.

## Design

1. Add a small token-resolution helper in `litellm/llms/github_copilot/authenticator.py` that checks:
   - `COPILOT_GITHUB_TOKEN`
   - `GH_TOKEN`
   - `GITHUB_TOKEN`

2. Update `get_access_token()` so the resolution order becomes:
   - explicit environment token
   - cached access-token file
   - device-code login

3. Leave `get_api_key()` and `_refresh_api_key()` intact so the legacy transport still exchanges the GitHub token for the short-lived Copilot API token it already expects.

4. Keep behavior backward compatible:
   - environment token present -> no device prompt
   - environment token absent but cached files exist -> reuse cache
   - neither available -> existing device-code flow

## Testing

Add focused tests for:

- environment token is returned by `get_access_token()` without touching device flow
- cached token file is still used when no environment token exists
- `get_api_key()` refresh path uses the environment-backed access token

## Documentation

Update the GitHub Copilot provider docs to state that legacy fallback paths now also respect `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` before device-code auth.
