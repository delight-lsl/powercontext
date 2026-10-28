"""Reset the local SDD context-management state.

The script offers the reset operations that the documentation describes, so
that a reviewer can return the environment to a known state without hunting
for the right SQL.  Destructive operations are opt-in and print what they are
about to do.

Usage::

    python scripts/reset.py --scope minimax-sdd      # drop a scope, re-import
    python scripts/reset.py --artifacts              # delete the recorded artefacts
    python scripts/reset.py --worktree               # restore the pre-change sample
    python scripts/reset.py --database               # drop and recreate the database
    python scripts/reset.py --all                    # scope + artefacts + worktree
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sddctx.parsing import scan_project  # noqa: E402
from sddctx.store import PowerContextStore, build_client  # noqa: E402

SAMPLE_DIR = ROOT / "sample"
ARTIFACTS_DIR = ROOT / "artifacts"
BEFORE_SNAPSHOT = ARTIFACTS_DIR / "snapshots" / "before"
DEFAULT_DATABASE = "powercontext_sdd"
MYSQL_CONTAINER = "oceanbase-seekdb"


def _say(message: str) -> None:
    print(message, flush=True)


def _mysql(sql: str, database: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run SQL against the seekdb container.

    The statement is fed through stdin because a reset can produce a script
    that exceeds the Windows command line length limit.
    """
    command = [
        "docker",
        "exec",
        "-i",
        MYSQL_CONTAINER,
        "mysql",
        "-h127.0.0.1",
        "-P2881",
        "-uroot",
        "-N",
    ]
    if database:
        command += ["-D", database]
    return subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _scope_tables(database: str) -> list[str]:
    """Every PowerContext table that carries a ``scope_id`` column."""
    completed = _mysql(
        "SELECT table_name FROM information_schema.columns "
        f"WHERE table_schema = '{database}' AND column_name = 'scope_id';",
        database,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def reset_scope(server_url: str, title: str, project: Path, database: str) -> int:
    """Delete a scope's rows from the database and import the project again.

    PowerContext does not expose a scope deletion endpoint, so the reset is done
    directly against the store.  Foreign key checks are disabled for the
    session because the scope's rows span several tables.
    """

    async def run() -> int:
        client = build_client(server_url)
        default_scope = await client.get_default_scope()
        store = PowerContextStore(client, default_scope.scope_id)
        deleted_scope = await store.ensure_scope(title, "MiniMax SDD context management corpus")

        tables = _scope_tables(database)
        if not tables:
            _say("could not resolve the PowerContext tables; is the database reachable?")
            return 1
        statements = ["SET FOREIGN_KEY_CHECKS=0;"]
        statements += [
            f"DELETE FROM `{database}`.`{table}` WHERE scope_id = '{deleted_scope}';" for table in tables
        ]
        statements.append("SET FOREIGN_KEY_CHECKS=1;")
        completed = _mysql("\n".join(statements), database)
        if completed.returncode != 0:
            _say(f"delete failed: {completed.stderr.strip()}")
            return 1

        # The scope row itself was part of the deletion, so the scope is
        # recreated with a fresh identifier before the corpus is imported again.
        scope_id = await store.ensure_scope(title, "MiniMax SDD context management corpus")
        fresh = PowerContextStore(client, scope_id)
        records = scan_project(project)
        entries = await fresh.put_records(records)
        _say(
            f"deleted scope {deleted_scope}, recreated it as {scope_id} "
            f"and re-imported {len(entries)} records"
        )
        return 0

    return asyncio.run(run())


def reset_artifacts() -> int:
    """Delete the recorded artefacts."""
    if not ARTIFACTS_DIR.exists():
        _say("no artefacts directory")
        return 0
    shutil.rmtree(ARTIFACTS_DIR)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    _say(f"deleted {ARTIFACTS_DIR}")
    return 0


def reset_worktree() -> int:
    """Restore the sample project from the recorded pre-change snapshot."""
    if not BEFORE_SNAPSHOT.exists():
        _say("no pre-change snapshot available; run scripts/run_e2e.py first")
        return 1
    for sub in ("src", "specs", "tests"):
        source = BEFORE_SNAPSHOT / sub
        if not source.exists():
            continue
        target = SAMPLE_DIR / sub
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    _say(f"restored {SAMPLE_DIR} from {BEFORE_SNAPSHOT}")
    return 0


def prune_demo_scopes(server_url: str, prefixes: tuple[str, ...], database: str) -> int:
    """Delete the throwaway scopes created by the verification scripts."""

    with urllib.request.urlopen(f"{server_url}/v1/scopes", timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    targets = [
        item for item in payload.get("items", []) if str(item.get("title", "")).startswith(prefixes)
    ]
    if not targets:
        _say("no demo scopes to prune")
        return 0
    tables = _scope_tables(database)
    statements = ["SET FOREIGN_KEY_CHECKS=0;"]
    for item in targets:
        scope_id = str(item.get("scope_id"))
        for table in tables:
            statements.append(
                f"DELETE FROM `{database}`.`{table}` WHERE scope_id = '{scope_id}';"
            )
        _say(f"pruned scope {scope_id} ({item.get('title')})")
    statements.append("SET FOREIGN_KEY_CHECKS=1;")
    completed = _mysql("\n".join(statements), database)
    if completed.returncode != 0:
        _say(f"prune failed: {completed.stderr.strip()}")
        return 1
    return 0


def reset_database(database: str) -> int:
    """Drop and recreate the PowerContext database."""
    drop = _mysql(f"DROP DATABASE IF EXISTS {database};")
    if drop.returncode != 0:
        _say(f"drop failed: {drop.stderr.strip()}")
        return 1
    create = _mysql(f"CREATE DATABASE {database} CHARACTER SET utf8mb4;")
    if create.returncode != 0:
        _say(f"create failed: {create.stderr.strip()}")
        return 1
    _say(f"recreated database {database}; restart PowerContext Server to rebuild the schema")
    return 0


def list_scopes(server_url: str) -> int:
    with urllib.request.urlopen(f"{server_url}/v1/scopes", timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for item in payload.get("items", []):
        _say(f"{item.get('scope_id')}  {item.get('title')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the local SDD context management state")
    parser.add_argument("--server-url", default="http://127.0.0.1:8010")
    parser.add_argument("--scope", default=None, help="Scope title to delete and re-import")
    parser.add_argument("--artifacts", action="store_true", help="Delete the recorded artefacts")
    parser.add_argument("--worktree", action="store_true", help="Restore the pre-change sample")
    parser.add_argument("--database", action="store_true", help="Drop and recreate the database")
    parser.add_argument("--database-name", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--prune-demo-scopes",
        action="store_true",
        help="Delete the throwaway scopes created by the verification scripts",
    )
    parser.add_argument("--list-scopes", action="store_true")
    parser.add_argument("--all", action="store_true", help="Scope + artefacts + worktree")
    parser.add_argument("--project", default=str(SAMPLE_DIR))
    args = parser.parse_args()

    project = Path(args.project).resolve()
    exit_code = 0

    if args.list_scopes:
        exit_code |= list_scopes(args.server_url)

    if args.all:
        exit_code |= reset_worktree()
        exit_code |= reset_artifacts()
        if args.scope:
            exit_code |= reset_scope(args.server_url, args.scope, project, args.database_name)
    else:
        if args.worktree:
            exit_code |= reset_worktree()
        if args.artifacts:
            exit_code |= reset_artifacts()
        if args.scope:
            exit_code |= reset_scope(args.server_url, args.scope, project, args.database_name)

    if args.prune_demo_scopes:
        exit_code |= prune_demo_scopes(
            args.server_url,
            ("minimax-sdd-restart-proof-", "minimax-sdd-spec-revision-", "minimax-sdd-reset-demo"),
            args.database_name,
        )
        # The API probes left a throwaway "Probe" scope behind; removing it keeps
        # the documented reproduction steps starting from a clean database.
        exit_code |= prune_demo_scopes(args.server_url, ("Probe",), args.database_name)

    if args.database:
        exit_code |= reset_database(args.database_name)

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
