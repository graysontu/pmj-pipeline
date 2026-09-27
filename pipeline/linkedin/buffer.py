"""Minimal client for Buffer's GraphQL API (https://developers.buffer.com).

Buffer holds the LinkedIn authorisation, so this repo never talks to LinkedIn
directly and needs only a Buffer API key (repo secret BUFFER_API_KEY). The
queries and the createPost mutation follow Buffer's documented examples
verbatim; values are inlined as GraphQL string literals.
"""

import json
from datetime import datetime, timezone

import httpx

API_URL = "https://api.buffer.com"


class BufferError(Exception):
    """Buffer refused a request or answered with something unexpected."""


def _literal(value: str) -> str:
    # A JSON string is a valid GraphQL string literal. ensure_ascii=False keeps
    # emoji as literal characters rather than surrogate-pair escapes.
    return json.dumps(value, ensure_ascii=False)


def buffer_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class BufferClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        if not api_key:
            raise BufferError("BUFFER_API_KEY is not set.")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=30.0)

    def _request(self, query: str) -> dict:
        response = self._http.post(
            API_URL,
            json={"query": query},
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        if response.status_code == 401:
            raise BufferError("Buffer rejected the API key (HTTP 401). Generate a new key and update the BUFFER_API_KEY secret.")
        if response.status_code != 200:
            raise BufferError(f"Buffer answered HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise BufferError(f"Buffer answered with non-JSON: {response.text[:300]}") from exc
        if body.get("errors"):
            details = "; ".join(
                f"{e.get('message')} ({(e.get('extensions') or {}).get('code', 'no code')})"
                for e in body["errors"]
            )
            raise BufferError(f"Buffer error: {details}")
        return body.get("data") or {}

    def channels(self) -> list[dict]:
        data = self._request("query GetOrganizations { account { organizations { id name } } }")
        organizations = (data.get("account") or {}).get("organizations") or []
        found = []
        for org in organizations:
            result = self._request(
                "query GetChannels { channels(input: { organizationId: "
                f"{_literal(org['id'])} }}) {{ id name service }} }}"
            )
            found += result.get("channels") or []
        return found

    def linkedin_channel(self, channel_id: str | None = None) -> dict:
        """The channel to post to: the one named by channel_id, or else the only
        LinkedIn channel on the account. Refuses to guess between several."""
        channels = self.channels()
        if channel_id:
            for channel in channels:
                if channel.get("id") == channel_id:
                    return channel
            raise BufferError(f"No Buffer channel has id {channel_id}.")
        linkedin = [c for c in channels if (c.get("service") or "").lower() == "linkedin"]
        if len(linkedin) == 1:
            return linkedin[0]
        if not linkedin:
            names = ", ".join(f"{c.get('name')} ({c.get('service')})" for c in channels) or "none"
            raise BufferError(f"No LinkedIn channel is connected in Buffer. Connected channels: {names}.")
        names = ", ".join(f"{c.get('name')} (id {c.get('id')})" for c in linkedin)
        raise BufferError(
            f"Several LinkedIn channels are connected in Buffer: {names}. "
            "Disconnect all but the company page, or set BUFFER_CHANNEL_ID."
        )

    def schedule_image_post(self, channel_id: str, text: str, image_url: str, due_at: datetime) -> dict:
        """Schedule one image post for due_at. Returns Buffer's post (id, dueAt)."""
        mutation = (
            "mutation CreatePost { createPost(input: { "
            f"text: {_literal(text)}, "
            f"channelId: {_literal(channel_id)}, "
            "schedulingType: automatic, "
            "mode: customScheduled, "
            f"dueAt: {_literal(buffer_timestamp(due_at))}, "
            f"assets: [{{ image: {{ url: {_literal(image_url)} }} }}] "
            "}) { "
            "... on PostActionSuccess { post { id dueAt } } "
            "... on MutationError { message } "
            "} }"
        )
        result = self._request(mutation).get("createPost") or {}
        if "post" in result and result["post"]:
            return result["post"]
        raise BufferError(f"Buffer did not create the post: {result.get('message') or result}")
