from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class PioneerError(RuntimeError):
    pass


class PioneerAuthError(PioneerError):
    pass


class PioneerRateLimited(PioneerError):
    pass


class PioneerClient:
    """Thin synchronous client for the Pioneer (Fastino) inference API.

    Docs: https://docs.pioneer.ai/concepts/inference
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.pioneer.ai",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise PioneerAuthError("PIONEER_API_KEY is empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, headers=self._headers(), json=payload)
        return self._handle_response(response, path)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(url, headers=self._headers(), params=params)
        return self._handle_response(response, path)

    @staticmethod
    def _handle_response(response: httpx.Response, path: str) -> dict[str, Any]:
        status = response.status_code
        if status == 401 or status == 403:
            raise PioneerAuthError(f"Pioneer auth failed at {path}: {response.text[:300]}")
        if status == 429:
            raise PioneerRateLimited(f"Pioneer rate limit at {path}: {response.text[:200]}")
        if status >= 400:
            raise PioneerError(f"Pioneer {status} at {path}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PioneerError(f"Pioneer non-JSON response at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PioneerError(f"Pioneer response at {path} is not an object")
        return data

    def infer(
        self,
        *,
        model_id: str,
        text: str,
        schema: dict[str, Any],
        threshold: float | None = None,
        store: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_id": model_id,
            "text": text,
            "schema": schema,
        }
        if threshold is not None:
            payload["threshold"] = threshold
        if store is False:
            payload["store"] = False
        return self._post("/inference", payload)

    def submit_feedback(
        self,
        inference_id: str,
        *,
        verdict: str,
        corrected_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if verdict not in {"correct", "incorrect"}:
            raise ValueError("verdict must be 'correct' or 'incorrect'")
        payload: dict[str, Any] = {"verdict": verdict}
        if corrected_output is not None:
            payload["corrected_output"] = corrected_output
        return self._post(f"/inferences/{inference_id}/feedback", payload)

    def base_models(self) -> dict[str, Any]:
        return self._get("/base-models")
