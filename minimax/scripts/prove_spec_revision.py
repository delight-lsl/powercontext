"""Prove that an edited specification changes the retrieved development context.

The script edits a real specification file in the repository, re-imports the
project into an isolated scope and compares the two context packs.  The
original file content is restored before the script exits, and the restore is
verified by comparing the SHA-256 of the file bytes.

The recorded artifact contains:

* the exact edit that was applied (as a unified diff);
* the context pack before and after the edit;
* the diff between the two packs, including the new ``source_id`` and
  ``db_position`` of every re-imported specification section;
* a direct SQL query proving that both the old and the new version of the
  specification are stored in the database.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIMAX_DIR = REPO_ROOT / "minimax"
sys.path.insert(0, str(MINIMAX_DIR))

from sddctx.parsing import scan_project  # noqa: E402
from sddctx.render import render_report  # noqa: E402
from sddctx.retrieval import build_context_pack  # noqa: E402
from sddctx.store import PowerContextStore, build_client  # noqa: E402

ARTIFACT_DIR = MINIMAX_DIR / "artifacts" / "spec-revision"
SAMPLE_DIR = MINIMAX_DIR / "sample"
SPEC_PATH = SAMPLE_DIR / "specs" / "SPEC-BILL-001-invoice-promo-discount.md"
TASK = "实现发票促销码折扣并保持金额不变量"

# The edit that a product owner would make after the first review round:
# a third promotion code and a raised discount cap for SAVE20.
OLD_ROW = "| `SAVE20` | 20 | 100.00 | 无 |"
NEW_ROW = "| `SAVE20` | 20 | 150.00 | 无 |"
OLD_EXAMPLE = "| `SAVE10` | 100.00 | 10.00 | 9.00 | 99.00 |"
NEW_EXAMPLE = "| `SAVE10` | 100.00 | 10.00 | 9.00 | 99.00 |\n| `SAVE30` | 100.00 | 30.00 | 7.00 | 77.00 |"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply_edit(original: str) -> str:
    """Apply the documented specification edit."""
    if OLD_ROW not in original or OLD_EXAMPLE not in original:
        raise RuntimeError("the specification no longer contains the expected text to patch")
    edited = original.replace(OLD_ROW, NEW_ROW)
    edited = edited.replace(
        "| `OLD15` | 15 | 30.00 | 2020-06-30（已过期） |",
        "| `OLD15` | 15 | 30.00 | 2020-06-30（已过期） |\n| `SAVE30` | 30 | 200.00 | 无 |",
    )
    edited = edited.replace(OLD_EXAMPLE, NEW_EXAMPLE)
    edited = edited.replace("version: 1", "version: 2", 1)
    return edited


def _sql(query: str) -> str:
    import subprocess

    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "oceanbase-seekdb",
                "mysql",
                "-h127.0.0.1",
                "-P2881",
                "-uroot",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"<unavailable: {error}>"
    return (completed.stdout or completed.stderr).strip()


async def _pack(store: PowerContextStore, task: str):
    corpus = await store.list_corpus()
    progress = await store.list_progress()
    return build_context_pack(task, corpus, scope_id=store.scope_id, progress=progress)


async def main_async(args: argparse.Namespace) -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    original_bytes = SPEC_PATH.read_bytes()
    original_text = original_bytes.decode("utf-8")
    original_sha = _sha256(original_bytes)

    client = build_client(args.server_url)
    default_scope = await client.get_default_scope()
    title = f"minimax-sdd-spec-revision-{int(time.time())}"
    scope_id = await PowerContextStore(client, default_scope.scope_id).ensure_scope(
        title, "Isolated scope used by the specification revision proof"
    )
    store = PowerContextStore(client, scope_id)

    steps: list[dict[str, object]] = []

    def record(name: str, command: str, exit_code: int, detail: object) -> None:
        steps.append(
            {
                "step": name,
                "command": command,
                "exit_code": exit_code,
                "detail": detail,
                "timestamp": _now(),
            }
        )

    # -- before -------------------------------------------------------------
    records = scan_project(SAMPLE_DIR)
    entries = await store.put_records(records)
    pack_before = await _pack(store, TASK)
    report_before = render_report(pack_before)
    record(
        "ingest-and-query-before-edit",
        f"python -m sddctx ingest --project {SAMPLE_DIR} && python -m sddctx query --task '{TASK}'",
        0,
        {
            "scope_id": scope_id,
            "record_count": len(records),
            "spec_sections": len(pack_before.applicable_specs),
            "files_to_modify": pack_before.files_to_modify,
        },
    )

    # -- edit ---------------------------------------------------------------
    edited_text = _apply_edit(original_text)
    edited_bytes = edited_text.encode("utf-8")
    SPEC_PATH.write_bytes(edited_bytes)
    edit_diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            edited_text.splitlines(keepends=True),
            fromfile=f"a/{SPEC_PATH.relative_to(REPO_ROOT).as_posix()}",
            tofile=f"b/{SPEC_PATH.relative_to(REPO_ROOT).as_posix()}",
        )
    )
    record(
        "edit-specification",
        f"patch {SPEC_PATH.relative_to(REPO_ROOT).as_posix()}",
        0,
        {
            "sha256_before": original_sha,
            "sha256_after": _sha256(edited_bytes),
            "diff": edit_diff,
        },
    )

    # -- re-import ----------------------------------------------------------
    records_after = scan_project(SAMPLE_DIR)
    entries_after = await store.put_records(records_after)
    pack_after = await _pack(store, TASK)
    report_after = render_report(pack_after)
    record(
        "reimport-and-query-after-edit",
        f"python -m sddctx ingest --project {SAMPLE_DIR} && python -m sddctx query --task '{TASK}'",
        0,
        {
            "record_count": len(records_after),
            "spec_sections": len(pack_after.applicable_specs),
            "files_to_modify": pack_after.files_to_modify,
        },
    )

    # -- compare ------------------------------------------------------------
    def spec_index(pack) -> dict[str, dict]:
        return {
            item.record_id: {
                "title": item.meta.get("title"),
                "lines": list(item.lines),
                "source_id": item.source_id,
                "db_position": item.db_position,
                "file_sha256": item.sha256,
                "text": item.text,
            }
            for item in pack.applicable_specs
        }

    before_index = spec_index(pack_before)
    after_index = spec_index(pack_after)
    changed = []
    for record_id, after in after_index.items():
        before = before_index.get(record_id)
        if before is None:
            changed.append({"record_id": record_id, "change": "new section", "after": after})
        elif before["source_id"] != after["source_id"]:
            changed.append(
                {
                    "record_id": record_id,
                    "change": "new source version",
                    "before": before,
                    "after": after,
                }
            )

    new_text_marker = "`SAVE30`"
    new_text_present = any(new_text_marker in item["text"] for item in after_index.values())
    old_text_present = any(new_text_marker in item["text"] for item in before_index.values())

    versions = _sql(
        "SELECT source_id, journal_position FROM powercontext_sdd.pc_sources "
        "WHERE scope_id = '%s' AND source_id LIKE 'sddctx:v1:spec_section:SPEC-BILL-001%%' "
        "ORDER BY journal_position;" % scope_id
    )

    # -- restore ------------------------------------------------------------
    SPEC_PATH.write_bytes(original_bytes)
    restored_sha = _sha256(SPEC_PATH.read_bytes())
    record(
        "restore-specification",
        f"restore {SPEC_PATH.relative_to(REPO_ROOT).as_posix()}",
        0,
        {"sha256_before_edit": original_sha, "sha256_after_restore": restored_sha},
    )

    # -- the production scope must be untouched -----------------------------
    main_store = PowerContextStore(client, default_scope.scope_id)
    main_pack = await _pack(main_store, TASK)
    main_text = " ".join(item.text for item in main_pack.applicable_specs)
    main_scope_clean = new_text_marker not in main_text

    verdict = {
        "spec_sections_before": len(pack_before.applicable_specs),
        "spec_sections_after": len(pack_after.applicable_specs),
        "respecified_sections": len(changed),
        "new_content_present_after_reimport": new_text_present,
        "new_content_absent_before_edit": not old_text_present,
        "source_ids_changed": bool(changed),
        "repository_restored": restored_sha == original_sha,
        "production_scope_untouched": main_scope_clean,
    }
    verdict["passed"] = all(
        verdict[key]
        for key in (
            "new_content_present_after_reimport",
            "new_content_absent_before_edit",
            "source_ids_changed",
            "repository_restored",
            "production_scope_untouched",
        )
    ) and verdict["respecified_sections"] > 0

    payload = {
        "schema": "sddctx.spec-revision-proof.v1",
        "generated_at": _now(),
        "scope_id": scope_id,
        "task": TASK,
        "spec_file": str(SPEC_PATH.relative_to(REPO_ROOT).as_posix()),
        "spec_sha256_before": original_sha,
        "spec_sha256_after_edit": _sha256(edited_bytes),
        "spec_sha256_after_restore": restored_sha,
        "verdict": verdict,
        "stored_source_versions": versions,
        "changed_sections": changed,
        "report_before": report_before,
        "report_after": report_after,
        "steps": steps,
    }
    (ARTIFACT_DIR / "spec-revision-proof.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ARTIFACT_DIR / "report-before.md").write_text(report_before + "\n", encoding="utf-8")
    (ARTIFACT_DIR / "report-after.md").write_text(report_after + "\n", encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove that an edited spec changes the context")
    parser.add_argument("--server-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
