# ruff: noqa: TRY003, TRY004
# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP reranking client for coarse Memory search candidates."""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

if TYPE_CHECKING:
    import httpx


def _validate_score_item(
    item: Any,
    expected_ranks: set[int],
    seen_ranks: set[int],
) -> tuple[int, float]:
    """Validate a single score entry from the HTTP reranker response."""
    if not isinstance(item, dict):
        raise ValueError("Each score item must be a dictionary")
    if set(item.keys()) != {"rank", "score"}:
        raise ValueError("Each score item must contain exactly 'rank' and 'score'")

    rank = item["rank"]
    if type(rank) is not int:
        raise ValueError(f"rank must be an integer, got {type(rank).__name__}")
    if rank not in expected_ranks:
        raise ValueError(f"rank {rank} is not in expected batch ranks")
    if rank in seen_ranks:
        raise ValueError(f"duplicate rank {rank} in response")

    score = item["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"score must be a numeric value, got {type(score).__name__}")
    if math.isnan(score) or math.isinf(score):
        raise ValueError("score must be a finite number, cannot be NaN or Infinity")

    seen_ranks.add(rank)
    return rank, float(score)


def _validate_response_data(
    data: Any,
    expected_ranks: set[int],
) -> list[tuple[int, float]]:
    """Validate the complete response body against the expected batch ranks."""
    if not isinstance(data, dict):
        raise ValueError(f"Response must be a JSON object, got {type(data).__name__}")
    if set(data.keys()) != {"scores"}:
        raise ValueError("Response must contain exactly the 'scores' key")

    scores_list = data["scores"]
    if not isinstance(scores_list, list):
        raise ValueError("'scores' must be a list")

    seen_ranks: set[int] = set()
    validated: list[tuple[int, float]] = []
    for item in scores_list:
        validated.append(_validate_score_item(item, expected_ranks, seen_ranks))

    if seen_ranks != expected_ranks:
        raise ValueError(f"Response did not cover all batch ranks: missing {expected_ranks - seen_ranks}")

    return validated


class HTTPMemoryReranker:
    """Select final Memory hits by scoring candidates with an external HTTP reranker."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")

        self.endpoint = endpoint
        self.client = client
        self._client = client
        self.batch_size = batch_size
        self.policy_id = policy_id

    async def _send_batch(
        self,
        query: str,
        batch: list[dict[str, Any]],
    ) -> list[tuple[int, float]]:
        """Send a single batch request and validate the response scores."""
        payload = {"query": query, "candidates": batch}
        response = await self._client.post(self.endpoint, json=payload)
        response.raise_for_status()

        try:
            data = response.json()
        except Exception as exc:
            raise ValueError(f"Invalid JSON response: {exc}") from exc

        return _validate_response_data(data, expected_ranks={item["rank"] for item in batch})

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score coarse candidates across HTTP batches and return top selected ranks."""
        if not isinstance(query, str) or len(query) < 1:
            raise ValueError("query must be a non-empty string")
        if not candidates:
            raise ValueError("rerank requires at least one candidate")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")

        batches: list[list[dict[str, Any]]] = [
            [
                {"rank": start_idx + i + 1, "text": candidate.text}
                for i, candidate in enumerate(candidates[start_idx : start_idx + self.batch_size])
            ]
            for start_idx in range(0, len(candidates), self.batch_size)
        ]

        batch_results = await asyncio.gather(*(self._send_batch(query, b) for b in batches))

        all_scores: list[tuple[int, float]] = []
        for res in batch_results:
            all_scores.extend(res)

        all_scores.sort(key=lambda item: (-item[1], item[0]))
        selected_ranks = tuple(rank for rank, _ in all_scores[:limit])

        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=len(batches)),
            discarded_rank_count=0,
            used_fallback=False,
        )


__all__ = ["HTTPMemoryReranker"]
