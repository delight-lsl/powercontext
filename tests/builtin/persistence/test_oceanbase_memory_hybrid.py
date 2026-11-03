# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import mysql

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
    _build_hybrid_search_sql,
)
from powercontext.builtin.tags import TagFilter

PROFILE = EmbeddingProfile(
    profile_id="test-hybrid-v1",
    model="deterministic",
    dimension=3,
    distance="l2",
    normalization="unit",
)


def test_build_hybrid_search_sql_structure() -> None:
    untagged = _build_hybrid_search_sql(tagged=False)
    assert "pc_memory_entry_heads" in untagged
    assert "pc_memory_vector_entries" in untagged
    assert "AGAINST" in untagged
    assert "l2_distance" in untagged
    assert "UNION ALL" in untagged
    assert "APPROXIMATE" in untagged
    assert "'fts' AS channel" in untagged
    assert "'vector' AS channel" in untagged

    tagged = _build_hybrid_search_sql(tagged=True)
    assert "pc_memory_entry_heads" in tagged
    assert "pc_memory_vector_entries" in tagged
    assert "AGAINST" in tagged
    assert "l2_distance" in tagged
    assert "UNION ALL" in tagged
    assert "APPROXIMATE" not in tagged
    assert "pc_artifact_tags" in tagged


def test_oceanbase_memory_vector_index_hybrid_search_execution() -> None:
    async def scenario() -> None:
        index = OceanBaseMemoryVectorIndex(PROFILE)
        ref = ArtifactRef(family="memory", artifact_id="art-1", revision=2)

        connection = AsyncMock()
        connection.dialect = mysql.dialect()

        mock_rows = [
            {
                "memory_artifact_id": "art-1",
                "head_revision": 1,
                "entry_id": "entry-alpha",
                "entry_version_id": "ver-alpha-1",
                "text": "Alpha lexical and semantic text.",
                "distance": None,
                "channel": "fts",
            },
            {
                "memory_artifact_id": "art-1",
                "head_revision": 1,
                "entry_id": "entry-alpha",
                "entry_version_id": "ver-alpha-1",
                "text": "Alpha lexical and semantic text.",
                "distance": 0.05,
                "channel": "vector",
            },
            {
                "memory_artifact_id": "art-1",
                "head_revision": 2,
                "entry_id": "entry-gamma",
                "entry_version_id": "ver-gamma-1",
                "text": "Gamma semantic text.",
                "distance": 0.1,
                "channel": "vector",
            },
        ]
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = mock_rows
        connection.execute.return_value = result_mock

        # Mock vector_complete to return True
        index.vector_complete = AsyncMock(return_value=True)  # type: ignore[method-assign]

        request = MemorySearchRequest(
            query="alpha",
            analyzed_query="alpha",
            memories=(ref,),
            candidate_limit=10,
            mode="hybrid",
            query_vector=(1.0, 0.0, 0.0),
            embedding_profile=PROFILE,
            tag_filter=None,
        )

        channels = await index.search_hybrid(cast(Any, connection), "scope-1", request)

        assert len(connection.execute.await_args_list) == 1
        executed_sql = str(connection.execute.await_args_list[0][0][0])
        assert "pc_memory_entry_heads" in executed_sql
        assert "pc_memory_vector_entries" in executed_sql
        assert "AGAINST" in executed_sql
        assert "l2_distance" in executed_sql

        assert len(channels.fts) == 1
        assert channels.fts[0].entry_id == "entry-alpha"
        assert channels.fts[0].text == "Alpha lexical and semantic text."
        assert channels.fts[0].distance is None
        assert channels.fts[0].memory_ref.revision == 2

        assert len(channels.vector) == 2
        assert channels.vector[0].entry_id == "entry-alpha"
        assert channels.vector[0].distance == 0.05
        assert channels.vector[0].memory_ref.revision == 2
        assert channels.vector[1].entry_id == "entry-gamma"
        assert channels.vector[1].distance == 0.1
        assert channels.vector[1].memory_ref.revision == 2

    asyncio.run(scenario())


def test_oceanbase_memory_vector_index_hybrid_search_with_tag_filter() -> None:
    async def scenario() -> None:
        index = OceanBaseMemoryVectorIndex(PROFILE)
        ref = ArtifactRef(family="memory", artifact_id="art-1", revision=1)

        connection = AsyncMock()
        connection.dialect = mysql.dialect()
        result_mock = MagicMock()
        result_mock.mappings.return_value.all.return_value = []
        connection.execute.return_value = result_mock
        index.vector_complete = AsyncMock(return_value=True)  # type: ignore[method-assign]

        request = MemorySearchRequest(
            query="test",
            analyzed_query="test",
            memories=(ref,),
            candidate_limit=5,
            mode="hybrid",
            query_vector=(0.0, 1.0, 0.0),
            embedding_profile=PROFILE,
            tag_filter=TagFilter(tags=("tag1", "tag2")),
        )

        channels = await index.search_hybrid(cast(Any, connection), "scope-1", request)
        assert len(connection.execute.await_args_list) == 1
        params = connection.execute.await_args_list[0][0][1]
        assert "tag_keys" in params
        assert "tag_hashes" in params
        assert "tag_minimum" in params
        assert channels.fts == ()
        assert channels.vector == ()

    asyncio.run(scenario())


def test_composite_memory_index_dispatches_single_hybrid_query() -> None:
    async def scenario() -> None:
        fts_index = OceanBaseMemoryFTSIndex()
        vector_index = OceanBaseMemoryVectorIndex(PROFILE)
        composite = CompositeMemoryIndex(fts_index, vector_index)

        fts_index.search = AsyncMock()  # type: ignore[method-assign]
        expected_channels = MemorySearchChannels()
        vector_index.search_hybrid = AsyncMock(return_value=expected_channels)  # type: ignore[method-assign]

        connection = AsyncMock()
        ref = ArtifactRef(family="memory", artifact_id="art-1", revision=1)
        request = MemorySearchRequest(
            query="query",
            analyzed_query="query",
            memories=(ref,),
            candidate_limit=10,
            mode="hybrid",
            query_vector=(1.0, 0.0, 0.0),
            embedding_profile=PROFILE,
        )

        channels = await composite.search(cast(Any, connection), "scope-1", request)
        assert channels is expected_channels
        vector_index.search_hybrid.assert_awaited_once_with(connection, "scope-1", request)
        fts_index.search.assert_not_awaited()

    asyncio.run(scenario())


def test_single_channel_modes_behave_normally() -> None:
    async def scenario() -> None:
        fts_index = OceanBaseMemoryFTSIndex()
        vector_index = OceanBaseMemoryVectorIndex(PROFILE)
        ref = ArtifactRef(family="memory", artifact_id="art-1", revision=1)
        connection = AsyncMock()

        # FTS mode on vector index returns empty
        vec_req = MemorySearchRequest(
            query="q",
            analyzed_query="q",
            memories=(ref,),
            candidate_limit=10,
            mode="fts",
        )
        vec_channels = await vector_index.search(cast(Any, connection), "scope-1", vec_req)
        assert vec_channels.fts == ()
        assert vec_channels.vector == ()

        # Vector mode on FTS index returns empty
        fts_req = MemorySearchRequest(
            query="q",
            analyzed_query="q",
            memories=(ref,),
            candidate_limit=10,
            mode="vector",
            query_vector=(1.0, 0.0, 0.0),
            embedding_profile=PROFILE,
        )
        fts_channels = await fts_index.search(cast(Any, connection), "scope-1", fts_req)
        assert fts_channels.fts == ()
        assert fts_channels.vector == ()

        # Hybrid search validates embedding profile
        invalid_req = MemorySearchRequest(
            query="q",
            analyzed_query="q",
            memories=(ref,),
            candidate_limit=10,
            mode="hybrid",
            query_vector=None,
            embedding_profile=None,
        )
        with pytest.raises(CapabilityNotSupportedError):
            await vector_index.search_hybrid(cast(Any, connection), "scope-1", invalid_req)

    asyncio.run(scenario())
