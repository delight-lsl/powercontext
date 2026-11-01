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

"""Evidence selection pipeline for extracting literal fragments within UTF-8 budgets."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from powercontext.artifacts import ArtifactRef
    from powercontext.builtin.runtime.prepared_context import (
        EvidenceFragment,
        PreparedMemoryCandidates,
    )

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

_STOP_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "up",
    "about",
    "into",
    "over",
    "after",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "can",
    "could",
    "will",
    "would",
    "shall",
    "should",
    "may",
    "might",
    "must",
    "that",
    "which",
    "who",
    "whom",
    "this",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "their",
    "we",
    "our",
    "you",
    "your",
}

_ABBREVIATIONS = {"al.", "e.g.", "i.e.", "vs.", "fig.", "dr.", "mr.", "ms.", "prof.", "etc."}

_SENTENCE_BOUNDARY_RE = re.compile(r"([.!?]['\"]?\s+|[\u3002\uff01\uff1f]\s*|\n+)")


def stem_word(word: str) -> str:
    """Lightweight rule-based English suffix stemmer."""
    w = word.lower()
    if len(w) <= 3:
        return w
    suffixes = (
        ("ities", "i"),
        ("ity", "i"),
        ("ations", "at"),
        ("ation", "at"),
        ("tions", "t"),
        ("tion", "t"),
        ("ssions", "ss"),
        ("ssion", "ss"),
        ("ments", ""),
        ("ment", ""),
        ("nesses", ""),
        ("ness", ""),
        ("ings", ""),
        ("ing", ""),
        ("ives", "iv"),
        ("ive", "iv"),
        ("able", ""),
        ("ible", ""),
        ("ies", "i"),
        ("ied", "i"),
        ("ised", "is"),
        ("ized", "iz"),
        ("ises", "is"),
        ("izes", "iz"),
        ("ed", ""),
        ("es", ""),
        ("ly", ""),
        ("al", ""),
    )
    for suf, rep in suffixes:
        if w.endswith(suf) and len(w) - len(suf) + len(rep) >= 3:
            return w[: -len(suf)] + rep
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def tokenize_text(text: str) -> list[str]:
    """Tokenize text into lowercase words, numbers, and CJK characters."""
    return _TOKEN_RE.findall(text.lower())


def split_sentences_with_spans(text: str) -> list[tuple[int, int, str]]:
    """Segment text into sentences, returning exact half-open [start, end) character spans."""
    spans: list[tuple[int, int, str]] = []
    last_end = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        m_start = match.start()
        m_end = match.end()
        prefix = text[last_end : m_start + 1].strip().lower()
        if any(prefix.endswith(abbrev) for abbrev in _ABBREVIATIONS):
            continue
        sub = text[last_end : m_start + 1]
        leading = len(sub) - len(sub.lstrip())
        trailing = len(sub) - len(sub.rstrip())
        s_idx = last_end + leading
        e_idx = m_start + 1 - trailing
        if s_idx < e_idx:
            spans.append((s_idx, e_idx, text[s_idx:e_idx]))
        last_end = m_end
    if last_end < len(text):
        sub = text[last_end:]
        leading = len(sub) - len(sub.lstrip())
        trailing = len(sub) - len(sub.rstrip())
        s_idx = last_end + leading
        e_idx = len(text) - trailing
        if s_idx < e_idx:
            spans.append((s_idx, e_idx, text[s_idx:e_idx]))
    return spans


@dataclass
class _CandidateSentence:
    scope_id: str
    memory_ref: ArtifactRef
    entry_id: str
    entry_version_id: str
    start: int
    end: int
    text: str
    doc_rank: int
    score: float = 0.0


def _extract_candidates(
    memory_candidates: Sequence[PreparedMemoryCandidates],
    current_scope_id: str | None,
) -> list[_CandidateSentence]:
    candidates: list[_CandidateSentence] = []
    doc_rank = 0
    seen_entries: set[tuple[str, str, str]] = set()

    for group in memory_candidates:
        scope_id = group.scope_id or current_scope_id or ""
        for hit in group.hits:
            entry_key = (scope_id, hit.entry_id, hit.entry_version_id)
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)

            text = hit.text
            if not text.strip():
                continue

            for start, end, sent_text in split_sentences_with_spans(text):
                candidates.append(
                    _CandidateSentence(
                        scope_id=scope_id,
                        memory_ref=hit.memory_ref,
                        entry_id=hit.entry_id,
                        entry_version_id=hit.entry_version_id,
                        start=start,
                        end=end,
                        text=sent_text,
                        doc_rank=doc_rank,
                    )
                )
            doc_rank += 1
    return candidates


def _bigram_bonus(
    tokens: list[str],
    stems: list[str],
    q_tokens: list[str],
    q_stems: list[str],
) -> float:
    bonus = 0.0
    for k in range(len(q_tokens) - 1):
        t1, t2 = q_tokens[k], q_tokens[k + 1]
        if t1 not in _STOP_WORDS or t2 not in _STOP_WORDS:
            for j in range(len(tokens) - 1):
                if (tokens[j] == t1 or stems[j] == q_stems[k]) and (
                    tokens[j + 1] == t2 or stems[j + 1] == q_stems[k + 1]
                ):
                    bonus += 3.0
                    break
    return bonus


def _score_single_sentence(
    tokens: list[str],
    stems: list[str],
    q_tokens: list[str],
    q_stems: list[str],
    non_stop_q: list[str],
    df: dict[str, int],
    n_candidates: int,
    avgdl: float,
    doc_rank: int,
) -> float:
    dl = len(tokens)
    bm25 = 0.0
    matched_non_stop = 0

    for qt, qs in zip(q_tokens, q_stems, strict=True):
        is_stop = qt in _STOP_WORDS
        n_q = df[qt]
        idf = math.log(1.0 + (n_candidates - n_q + 0.5) / (n_q + 0.5))
        if is_stop:
            idf *= 0.1

        tf = tokens.count(qt)
        is_stem = False
        if tf == 0:
            tf = stems.count(qs)
            is_stem = True

        if tf > 0:
            if not is_stop:
                matched_non_stop += 1
            weight = 0.75 if is_stem else 1.0
            k1 = 1.2
            b = 0.75
            denom = tf + k1 * (1.0 - b + b * (dl / max(1.0, avgdl)))
            bm25 += weight * idf * (tf * (k1 + 1.0)) / denom

    cov = matched_non_stop / max(1, len(non_stop_q))
    score = bm25 * (1.0 + 3.0 * (cov**2))
    score += _bigram_bonus(tokens, stems, q_tokens, q_stems)

    for qt in q_tokens:
        if qt.isdigit() and qt in tokens:
            score += 4.0

    return score / (1.0 + 0.05 * doc_rank)


def _score_candidates(
    candidates: list[_CandidateSentence],
    query: str,
) -> list[_CandidateSentence]:
    q_tokens = tokenize_text(query)
    q_stems = [stem_word(t) for t in q_tokens]
    non_stop_q = [t for t in q_tokens if t not in _STOP_WORDS] or q_tokens

    n_candidates = len(candidates)
    sent_tokens_list = [tokenize_text(c.text) for c in candidates]
    sent_stems_list = [[stem_word(t) for t in tokens] for tokens in sent_tokens_list]
    avgdl = sum(len(t) for t in sent_tokens_list) / max(1, n_candidates)

    df: dict[str, int] = {}
    for qt, qs in zip(q_tokens, q_stems, strict=True):
        df[qt] = sum(
            1 for tokens, stems in zip(sent_tokens_list, sent_stems_list, strict=True) if qt in tokens or qs in stems
        )

    for i, c in enumerate(candidates):
        c.score = _score_single_sentence(
            sent_tokens_list[i],
            sent_stems_list[i],
            q_tokens,
            q_stems,
            non_stop_q,
            df,
            n_candidates,
            avgdl,
            c.doc_rank,
        )

    by_entry: dict[tuple[str, str, str], list[_CandidateSentence]] = {}
    for c in candidates:
        by_entry.setdefault((c.scope_id, c.entry_id, c.entry_version_id), []).append(c)

    for entry_spans in by_entry.values():
        base_scores = [s.score for s in entry_spans]
        for i, s in enumerate(entry_spans):
            left = base_scores[i - 1] if i > 0 else 0.0
            right = base_scores[i + 1] if i < len(entry_spans) - 1 else 0.0
            s.score += 0.25 * max(left, right)

    valid_candidates = [c for c in candidates if c.score > 1e-6]
    valid_candidates.sort(key=lambda s: s.score, reverse=True)
    return valid_candidates


def _pack_within_budget(
    candidates: list[_CandidateSentence],
    max_bytes: int,
    trust_policy: str,
    begin_marker: str,
    end_marker: str,
) -> tuple[str | None, tuple[EvidenceFragment, ...]]:
    from powercontext.artifacts import ArtifactAddress
    from powercontext.builtin.runtime.prepared_context import EvidenceFragment, MemoryEntryAddress

    def render_envelope(items: list[dict[str, Any]]) -> str:
        envelope = {"trust": "untrusted_history", "items": items}
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        return "\n\n".join((trust_policy, f"{begin_marker}\n{encoded}\n{end_marker}"))

    def make_item(c: _CandidateSentence) -> dict[str, Any]:
        return {
            "citation": {
                "scope_id": c.scope_id,
                "memory_ref": c.memory_ref.model_dump(mode="json"),
                "entry_id": c.entry_id,
                "entry_version_id": c.entry_version_id,
            },
            "content": c.text,
            "start": c.start,
            "end": c.end,
        }

    selected_candidates: list[_CandidateSentence] = []
    selected_items: list[dict[str, Any]] = []

    for c in candidates:
        item = make_item(c)
        trial_items = [*selected_items, item]
        trial_content = render_envelope(trial_items)
        if len(trial_content.encode("utf-8")) <= max_bytes:
            selected_candidates.append(c)
            selected_items.append(item)

    if not selected_candidates:
        return None, ()

    final_content = render_envelope(selected_items)
    fragments = tuple(
        EvidenceFragment(
            address=MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=c.scope_id, artifact=c.memory_ref),
                entry_id=c.entry_id,
                entry_version_id=c.entry_version_id,
            ),
            start=c.start,
            end=c.end,
            text=c.text,
        )
        for c in selected_candidates
    )
    return final_content, fragments


def select_evidence(
    *,
    query: str,
    memory_candidates: Sequence[PreparedMemoryCandidates],
    max_bytes: int,
    trust_policy: str,
    begin_marker: str,
    end_marker: str,
    current_scope_id: str | None = None,
) -> tuple[str | None, tuple[EvidenceFragment, ...]]:
    """Select literal evidence fragments from memory candidates to satisfy UTF-8 byte budget."""
    candidates = _extract_candidates(memory_candidates, current_scope_id)
    if not candidates:
        return None, ()

    scored_candidates = _score_candidates(candidates, query)
    return _pack_within_budget(
        scored_candidates,
        max_bytes,
        trust_policy,
        begin_marker,
        end_marker,
    )
