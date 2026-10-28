"""Command line entry point of the SDD context manager.

    python -m sddctx ingest  --project <dir>
    python -m sddctx query   --task "<development task>"
    python -m sddctx prompt  --task "<development task>" --out prompt.md
    python -m sddctx state   save --kind progress --text "..."
    python -m sddctx state   list
    python -m sddctx minimax --task "<development task>" [--apply]
    python -m sddctx apply   --response <file> --project <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .minimax_client import MiniMaxClient, MiniMaxConfig, MiniMaxError
from .models import Record
from .parsing import scan_project
from .render import SYSTEM_PROMPT, render_prompt, render_report
from .retrieval import build_context_pack
from .store import PowerContextStore, build_client

DEFAULT_SERVER_URL = "http://127.0.0.1:8010"
DEFAULT_SCOPE_TITLE = "minimax-sdd"
DEFAULT_SCOPE_SUMMARY = "MiniMax SDD context management corpus"
FILE_MARKER = re.compile(r"^===\s*FILE:\s*(?P<path>.+?)\s*===\s*$", re.MULTILINE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _server_url(args: argparse.Namespace) -> str:
    return args.server_url or os.environ.get("SDDCTX_SERVER_URL", DEFAULT_SERVER_URL)


async def _store(args: argparse.Namespace) -> PowerContextStore:
    client = build_client(_server_url(args), token=os.environ.get("POWERCONTEXT_AUTH_TOKEN"))
    scope_id = (await client.get_default_scope()).scope_id
    title = args.scope_title or os.environ.get("SDDCTX_SCOPE_TITLE", DEFAULT_SCOPE_TITLE)
    store = PowerContextStore(client, scope_id)
    resolved = await store.ensure_scope(title, DEFAULT_SCOPE_SUMMARY)
    if resolved != scope_id:
        store = PowerContextStore(client, resolved)
    return store


def _write_json(path: str | None, payload: Any) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def command_ingest(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()

    async def run() -> int:
        store = await _store(args)
        records = scan_project(project)
        if not records:
            print("no records found", file=sys.stderr)
            return 2
        entries = await store.put_records(records)
        summary = {
            "generated_at": _now(),
            "project": str(project),
            "scope_id": store.scope_id,
            "records": len(records),
            "by_kind": {
                kind: sum(1 for record in records if record.kind == kind)
                for kind in ("spec_section", "code_symbol", "test_case")
            },
            "captured": [
                {
                    "source_id": entry.source_id,
                    "db_position": entry.position,
                    "server_content_digest": entry.content_digest,
                    "record_id": entry.record.record_id,
                    "file": entry.record.file,
                    "lines": list(entry.record.lines),
                }
                for entry in entries
            ],
        }
        _write_json(args.json, summary)
        print(
            f"ingested {len(records)} records into scope {store.scope_id} "
            f"({summary['by_kind']['spec_section']} spec sections, "
            f"{summary['by_kind']['code_symbol']} code symbols, "
            f"{summary['by_kind']['test_case']} test cases)"
        )
        return 0

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# query / prompt
# ---------------------------------------------------------------------------


async def _pack_for_task(args: argparse.Namespace, task: str):
    store = await _store(args)
    corpus = await store.list_corpus()
    progress = await store.list_progress()
    return store, build_context_pack(task, corpus, scope_id=store.scope_id, progress=progress)


def command_query(args: argparse.Namespace) -> int:
    async def run() -> int:
        store, pack = await _pack_for_task(args, args.task)
        report = render_report(pack)
        print(report)
        if args.markdown:
            target = Path(args.markdown)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report + "\n", encoding="utf-8")
        _write_json(args.json, pack.to_json())
        return 0

    return asyncio.run(run())


def command_prompt(args: argparse.Namespace) -> int:
    async def run() -> int:
        _store, pack = await _pack_for_task(args, args.task)
        prompt = render_prompt(pack, args.task)
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(prompt, encoding="utf-8")
        if not args.out or args.stdout:
            print(prompt)
        return 0

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def command_state(args: argparse.Namespace) -> int:
    async def run() -> int:
        store = await _store(args)
        if args.action == "save":
            entry = await store.remember_progress(args.kind, args.text, args.reason)
            print(json.dumps(entry.__dict__, ensure_ascii=False))
            return 0
        if args.action == "list":
            entries = await store.list_progress(include_inactive=args.include_inactive)
            payload = [entry.__dict__ for entry in entries]
            _write_json(args.json, payload)
            for entry in entries:
                print(f"[{entry.kind}] {entry.text} (artifact={entry.artifact_id}, state={entry.state})")
            return 0
        if args.action == "search":
            entries = await store.search_progress(args.text, limit=args.limit)
            for entry in entries:
                print(f"[{entry.kind}] {entry.text} (artifact={entry.artifact_id})")
            return 0
        return 2

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# minimax
# ---------------------------------------------------------------------------


def parse_model_files(text: str) -> dict[str, str]:
    """Split a model answer into ``{path: content}`` blocks."""
    matches = list(FILE_MARKER.finditer(text))
    files: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        body = body.strip("\n")
        if body.endswith("```"):
            body = body[: -3].rstrip("\n")
        files[match.group("path").strip()] = body + "\n"
    return files


def command_minimax(args: argparse.Namespace) -> int:
    async def run() -> int:
        store, pack = await _pack_for_task(args, args.task)
        prompt = render_prompt(pack, args.task)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        request_log = {
            "generated_at": _now(),
            "task": args.task,
            "scope_id": store.scope_id,
            "context_pack": pack.to_json(),
            "prompt": prompt,
            "system_prompt": SYSTEM_PROMPT,
            "api_key": "<redacted>",
            "note": "The API key is never stored. The model's private reasoning trace is discarded.",
        }
        _write_json(str(out_dir / "request.json"), request_log)

        try:
            config = MiniMaxConfig.from_env(model=args.model, base_url=args.base_url)
        except MiniMaxError as error:
            _write_json(str(out_dir / "response.json"), {"error": str(error), "generated_at": _now()})
            print(f"error: {error}", file=sys.stderr)
            return 3

        request_log["model"] = config.model
        request_log["base_url"] = config.base_url
        request_log["api_style"] = config.api_style
        _write_json(str(out_dir / "request.json"), request_log)

        client = MiniMaxClient(config)
        try:
            response = await client.complete(SYSTEM_PROMPT, prompt)
        except MiniMaxError as error:
            failure = {
                "generated_at": _now(),
                "status": "failed",
                "model": config.model,
                "base_url": config.base_url,
                "error": str(error),
                "error_code": error.status_code,
                "http_status": error.http_status,
                "error_payload": error.payload,
            }
            _write_json(str(out_dir / "response.json"), failure)
            print(
                f"MiniMax call failed: {error} (code={error.status_code}, http={error.http_status})",
                file=sys.stderr,
            )
            return 4

        files = parse_model_files(response.content)
        _write_json(
            str(out_dir / "response.json"),
            {
                "generated_at": _now(),
                "status": "ok",
                "model": response.model,
                "base_url": config.base_url,
                "usage": response.usage,
                "finish_reason": response.finish_reason,
                "request_id": response.request_id,
                "api_status": response.api_status,
                "content": response.content,
                "files": sorted(files),
            },
        )

        if args.apply and files:
            project = Path(args.project).resolve()
            diffs: list[str] = []
            for relative, content in files.items():
                target = project / relative
                original = target.read_text(encoding="utf-8") if target.exists() else ""
                diffs.append(
                    "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            content.splitlines(keepends=True),
                            fromfile=f"a/{relative}",
                            tofile=f"b/{relative}",
                        )
                    )
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            _write_json(str(out_dir / "applied.json"), {"files": sorted(files), "generated_at": _now()})
            (out_dir / "code.diff").write_text("\n".join(diffs), encoding="utf-8")
            print(f"applied {len(files)} file(s) from the model answer")
        print(f"model answer stored in {out_dir}")
        return 0

    return asyncio.run(run())


def command_apply(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.response).read_text(encoding="utf-8"))
    content = payload.get("content") or ""
    files = parse_model_files(content)
    if not files:
        print("no file blocks found in the model answer", file=sys.stderr)
        return 2
    project = Path(args.project).resolve()
    diffs: list[str] = []
    for relative, body in files.items():
        target = project / relative
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        diffs.append(
            "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    body.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    if args.diff:
        Path(args.diff).parent.mkdir(parents=True, exist_ok=True)
        Path(args.diff).write_text("\n".join(diffs), encoding="utf-8")
    print(f"applied {len(files)} file(s)")
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sddctx", description="SDD context management on PowerContext")
    parser.add_argument("--server-url", default=None, help="PowerContext Server base URL")
    parser.add_argument("--scope-title", default=None, help="Scope title used for the corpus")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Index a project into PowerContext")
    ingest.add_argument("--project", required=True)
    ingest.add_argument("--json", default=None, help="Write the ingest report to this path")
    ingest.set_defaults(func=command_ingest)

    query = sub.add_parser("query", help="Retrieve the development context for a task")
    query.add_argument("--task", required=True)
    query.add_argument("--json", default=None)
    query.add_argument("--markdown", default=None)
    query.set_defaults(func=command_query)

    prompt = sub.add_parser("prompt", help="Render the model prompt for a task")
    prompt.add_argument("--task", required=True)
    prompt.add_argument("--out", default=None)
    prompt.add_argument("--stdout", action="store_true")
    prompt.set_defaults(func=command_prompt)

    state = sub.add_parser("state", help="Read or write task progress in PowerContext")
    state_sub = state.add_subparsers(dest="action", required=True)
    save = state_sub.add_parser("save")
    save.add_argument("--kind", default="progress")
    save.add_argument("--text", required=True)
    save.add_argument("--reason", default=None)
    list_parser = state_sub.add_parser("list")
    list_parser.add_argument("--include-inactive", action="store_true")
    list_parser.add_argument("--json", default=None)
    search = state_sub.add_parser("search")
    search.add_argument("--text", required=True)
    search.add_argument("--limit", type=int, default=10)
    state.set_defaults(func=command_state)

    minimax = sub.add_parser("minimax", help="Send the retrieved context to MiniMax")
    minimax.add_argument("--task", required=True)
    minimax.add_argument("--project", default=None, help="Project root, required with --apply")
    minimax.add_argument("--model", default=None)
    minimax.add_argument("--base-url", default=None)
    minimax.add_argument("--out-dir", default="artifacts/minimax")
    minimax.add_argument("--apply", action="store_true")
    minimax.set_defaults(func=command_minimax)

    apply_parser = sub.add_parser("apply", help="Apply file blocks from a stored model answer")
    apply_parser.add_argument("--response", required=True)
    apply_parser.add_argument("--project", required=True)
    apply_parser.add_argument("--diff", default=None)
    apply_parser.set_defaults(func=command_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
