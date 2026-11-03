# ruff: noqa: TRY003
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

"""Concurrent Embedding request coalescing, deduplication, and bounded LRU vector caching."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile


@dataclass(slots=True)
class _BatchTracker:
    batch_id: int
    texts: tuple[str, ...]
    future: asyncio.Future[InferenceUsage]


@dataclass(slots=True)
class _InFlightText:
    future: asyncio.Future[EmbeddingVector]
    batch_tracker: _BatchTracker


class _PendingText:
    __slots__ = ("batch_tracker", "future")

    def __init__(self, future: asyncio.Future[EmbeddingVector]) -> None:
        self.future = future
        self.batch_tracker: _BatchTracker | None = None


def _aggregate_usage(trackers: Iterable[_BatchTracker]) -> InferenceUsage:
    """Aggregate provider usage across distinct underlying batches waited on by one call."""
    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    has_tokens = False

    for tracker in trackers:
        u = tracker.future.result()
        requests += u.requests
        if u.input_tokens is not None:
            input_tokens = (input_tokens or 0) + u.input_tokens
            has_tokens = True
        if u.output_tokens is not None:
            output_tokens = (output_tokens or 0) + u.output_tokens
            has_tokens = True

    if not has_tokens and requests == 0:
        return InferenceUsage(requests=0, input_tokens=None, output_tokens=None)

    return InferenceUsage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _validate_model_params(max_batch_size: int, coalesce_ms: float, cache_size: int) -> None:
    if type(max_batch_size) is not int or isinstance(max_batch_size, bool) or max_batch_size < 1:
        raise ValueError("max_batch_size must be a strictly positive integer")
    if type(coalesce_ms) not in (int, float) or isinstance(coalesce_ms, bool) or coalesce_ms < 0:
        raise ValueError("coalesce_ms must be a non-negative number")
    if type(cache_size) is not int or isinstance(cache_size, bool) or cache_size < 0:
        raise ValueError("cache_size must be a non-negative integer")


def _verify_vector_count(vectors: tuple[Any, ...], texts: tuple[str, ...]) -> None:
    if len(vectors) != len(texts):
        raise ValueError(f"Underlying model returned {len(vectors)} vectors for {len(texts)} texts")


class CoalescingEmbeddingModel:
    """Wrap an embedding model to coalesce concurrent requests and cache vectors in a bounded LRU."""

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        _validate_model_params(max_batch_size, coalesce_ms, cache_size)

        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_s = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size

        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._in_flight: dict[str, _InFlightText] = {}
        self._pending: dict[str, _PendingText] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._running_tasks: set[asyncio.Task[Any]] = set()
        self._batch_counter = 0

    @property
    def profile(self) -> EmbeddingProfile:
        """Expose the underlying model's embedding profile."""
        return self._model.profile

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

    def _resolve_one_text(
        self,
        text: str,
        loop: asyncio.AbstractEventLoop,
        cached_vectors: dict[str, EmbeddingVector],
        needed_futures: dict[str, asyncio.Future[EmbeddingVector]],
        text_trackers: dict[str, _BatchTracker],
        pending_items: dict[str, _PendingText],
    ) -> bool:
        if self._cache_size > 0 and text in self._cache:
            vec = self._cache[text]
            self._cache.move_to_end(text)
            cached_vectors[text] = vec
            return False
        if text in self._in_flight:
            inf = self._in_flight[text]
            needed_futures[text] = inf.future
            text_trackers[text] = inf.batch_tracker
            return False
        if text in self._pending:
            pending = self._pending[text]
            needed_futures[text] = pending.future
            pending_items[text] = pending
            return False

        fut: asyncio.Future[EmbeddingVector] = loop.create_future()
        pending = _PendingText(future=fut)
        self._pending[text] = pending
        needed_futures[text] = fut
        pending_items[text] = pending
        return True

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed an ordered text batch with request coalescing, deduplication, and LRU caching."""
        texts_tuple = tuple(texts)
        if not texts_tuple:
            return EmbeddingResult(
                vectors=(),
                usage=InferenceUsage(requests=0, input_tokens=None, output_tokens=None),
            )

        cached_vectors: dict[str, EmbeddingVector] = {}
        needed_futures: dict[str, asyncio.Future[EmbeddingVector]] = {}
        text_trackers: dict[str, _BatchTracker] = {}
        pending_items: dict[str, _PendingText] = {}
        new_pending_added = False
        loop = asyncio.get_running_loop()

        for text in texts_tuple:
            if text in cached_vectors or text in needed_futures:
                continue
            added = self._resolve_one_text(text, loop, cached_vectors, needed_futures, text_trackers, pending_items)
            if added:
                new_pending_added = True

        if new_pending_added and (self._dispatch_task is None or self._dispatch_task.done()):
            self._dispatch_task = asyncio.create_task(self._scheduled_dispatch())
            self._track_task(self._dispatch_task)

        if needed_futures:
            await asyncio.gather(*(asyncio.shield(fut) for fut in needed_futures.values()))
            for text, fut in needed_futures.items():
                cached_vectors[text] = fut.result()
                if text in pending_items:
                    tracker = pending_items[text].batch_tracker
                    if tracker is not None:
                        text_trackers[text] = tracker

        distinct_trackers: dict[int, _BatchTracker] = {
            tracker.batch_id: tracker for text in needed_futures if (tracker := text_trackers.get(text)) is not None
        }

        usage = _aggregate_usage(distinct_trackers.values())
        vectors = tuple(cached_vectors[text] for text in texts_tuple)
        return EmbeddingResult(vectors=vectors, usage=usage)

    async def _scheduled_dispatch(self) -> None:
        """Wait for the coalescing window to lapse, then dispatch pending texts."""
        if self._coalesce_s > 0:
            await asyncio.sleep(self._coalesce_s)
        else:
            await asyncio.sleep(0)
        self._dispatch()

    def _dispatch(self) -> None:
        """Chunk all pending texts into batches and dispatch them asynchronously."""
        if not self._pending:
            return

        items = list(self._pending.items())
        self._pending.clear()
        self._dispatch_task = None
        loop = asyncio.get_running_loop()

        for i in range(0, len(items), self._max_batch_size):
            slice_items = items[i : i + self._max_batch_size]
            batch_texts = tuple(text for text, _ in slice_items)
            self._batch_counter += 1
            batch_id = self._batch_counter
            tracker_fut: asyncio.Future[InferenceUsage] = loop.create_future()
            tracker = _BatchTracker(batch_id=batch_id, texts=batch_texts, future=tracker_fut)

            batch_futures: dict[str, asyncio.Future[EmbeddingVector]] = {}
            for text, pending in slice_items:
                pending.batch_tracker = tracker
                self._in_flight[text] = _InFlightText(future=pending.future, batch_tracker=tracker)
                batch_futures[text] = pending.future

            task = asyncio.create_task(self._run_batch(tracker, batch_texts, batch_futures))
            self._track_task(task)

    def _record_success(
        self,
        batch_texts: tuple[str, ...],
        result: EmbeddingResult,
        batch_futures: dict[str, asyncio.Future[EmbeddingVector]],
        tracker: _BatchTracker,
    ) -> None:
        if self._cache_size > 0:
            for text, vec in zip(batch_texts, result.vectors, strict=True):
                self._cache[text] = vec
                self._cache.move_to_end(text)
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)

        for text, vec in zip(batch_texts, result.vectors, strict=True):
            fut = batch_futures[text]
            if not fut.done():
                fut.set_result(vec)

        usage = result.usage if result.usage is not None else InferenceUsage(requests=1)
        tracker.future.set_result(usage)

    def _record_failure(
        self,
        batch_texts: tuple[str, ...],
        error: BaseException,
        batch_futures: dict[str, asyncio.Future[EmbeddingVector]],
        tracker: _BatchTracker,
    ) -> None:
        for text in batch_texts:
            fut = batch_futures[text]
            if not fut.done():
                fut.set_exception(error)
        if not tracker.future.done():
            tracker.future.set_exception(error)

    async def _run_batch(
        self,
        tracker: _BatchTracker,
        batch_texts: tuple[str, ...],
        batch_futures: dict[str, asyncio.Future[EmbeddingVector]],
    ) -> None:
        """Execute one underlying provider batch and resolve/fail waiting futures."""
        try:
            result = await self._model.embed(batch_texts)
            _verify_vector_count(result.vectors, batch_texts)
            self._record_success(batch_texts, result, batch_futures, tracker)
        except BaseException as error:
            self._record_failure(batch_texts, error, batch_futures, tracker)
        finally:
            for text in batch_texts:
                self._in_flight.pop(text, None)


__all__ = [
    "CoalescingEmbeddingModel",
]
