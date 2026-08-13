from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .storage import Storage


@dataclass
class ApiError(Exception):
    platform: str
    status_code: int | None
    message: str
    retry_after: float | None = None

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code == 429 or (self.status_code is not None and self.status_code >= 500)


class RateLimitedClient:
    def __init__(self, platform: str, base_url: str, token: str, rpm: int, storage: Storage):
        self.platform, self.rpm, self.storage = platform, rpm, storage
        self.http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        wait = self.storage.consume_token(self.platform, self.rpm)
        if wait > 0:
            time.sleep(wait)
            # A sleep alone does not consume a token, so claim it after waiting.
            self.storage.consume_token(self.platform, self.rpm)
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ApiError(self.platform, None, str(exc)) from exc
        if response.is_success:
            return response.json() if response.content else None
        retry_after = _retry_after(response)
        try:
            body = response.json()
            message = body.get("error", body.get("message", response.text))
            retry_after = retry_after or body.get("error_extra", {}).get("retry_after")
        except ValueError:
            message = response.text
        raise ApiError(self.platform, response.status_code, message, float(retry_after) if retry_after else None)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None


class TodoistClient(RateLimitedClient):
    def __init__(self, token: str, rpm: int, storage: Storage):
        super().__init__("todoist", "https://api.todoist.com/api/v1", token, rpm, storage)

    def _all(self, path: str) -> list[dict[str, Any]]:
        cursor: str | None = None
        output: list[dict[str, Any]] = []
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = self.request("GET", path, params=params)
            output.extend(response.get("results", response if isinstance(response, list) else []))
            cursor = response.get("next_cursor") if isinstance(response, dict) else None
            if not cursor:
                return output

    def active_tasks(self) -> list[dict[str, Any]]:
        return self._all("/tasks")

    def projects(self) -> list[dict[str, Any]]:
        return self._all("/projects")

    def delete_task(self, task_id: str) -> None:
        self.request("DELETE", f"/tasks/{task_id}")


class WorkflowyClient(RateLimitedClient):
    def __init__(self, key: str, rpm: int, storage: Storage):
        super().__init__("workflowy", "https://workflowy.com/api/v1", key, rpm, storage)

    def list_root_nodes(self) -> list[dict[str, Any]]:
        return self.request("GET", "/nodes", params={"parent_id": "None"}).get("nodes", [])

    def create_node(self, parent_id: str | None, name: str) -> str:
        response = self.request("POST", "/nodes", json={"parent_id": parent_id, "name": name, "position": "bottom"})
        return response["item_id"]

    def update_node(self, node_id: str, name: str, note: str) -> None:
        self.request("POST", f"/nodes/{node_id}", json={"name": name, "note": note, "layoutMode": "todo"})
