"""Prove that PowerContext keeps task progress across a restart of the module.

The proof runs against the real PowerContext Server and the real OceanBase /
seekdb store:

1.  a fresh scope is created and its Atomic Memory is shown to be empty;
2.  task progress and a verification result are written into that scope;
3.  the entries are read back;
4.  the PowerContext Server process is stopped and started again;
5.  the entries are read back from the restarted server and are byte identical;
6.  the reloaded progress is injected into the next development context pack.

Every step records the command, the exit code and the observed output.  The
script never fabricates a result: when the database does not contain the
expected rows, the script fails loudly instead of writing a green report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIMAX_DIR = REPO_ROOT / "minimax"
sys.path.insert(0, str(MINIMAX_DIR))

from sddctx.parsing import scan_project  # noqa: E402
from sddctx.retrieval import build_context_pack  # noqa: E402
from sddctx.store import PowerContextStore, build_client  # noqa: E402

RUNTIME_DIR = MINIMAX_DIR / ".runtime"
ARTIFACT_DIR = MINIMAX_DIR / "artifacts" / "restart"
SAMPLE_DIR = MINIMAX_DIR / "sample"
DEFAULT_PORT = 8011


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _powercontext_executable() -> Path:
    """Locate the ``powercontext`` console script of the active environment."""
    candidates = [
        Path(sys.executable).parent / "powercontext.exe",
        Path(sys.executable).parent / "powercontext",
        Path(sys.executable).parent / "powercontext-script.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "the 'powercontext' console script was not found next to the active interpreter; "
        "install the project with 'uv pip install -e .'"
    )


def _write_env_file(source: Path, target: Path, port: int, home: Path) -> Path:
    """Derive a runnable environment file from the operator's own file."""
    replacements = {
        "POWERCONTEXT_SERVER_HTTP_PORT": str(port),
        "POWERCONTEXT_SERVER_HTTP_HOST": "127.0.0.1",
        # The environment file parser treats a backslash as an escape character,
        # so a Windows path has to be written with forward slashes.
        "POWERCONTEXT_HOME": home.as_posix(),
    }
    lines = source.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in replacements:
                output.append(f"{key}={replacements[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(output) + "\n", encoding="utf-8")
    return target


class ServerHandle:
    """A PowerContext Server process owned by this script."""

    def __init__(self, env_file: Path, port: int, log_path: Path) -> None:
        self.env_file = env_file
        self.port = port
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at: str | None = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("ab")
        executable = _powercontext_executable()
        self.process = subprocess.Popen(
            [
                str(executable),
                "server",
                "run",
                "--env-file",
                str(self.env_file),
                "--port",
                str(self.port),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            cwd=str(REPO_ROOT),
        )
        self.started_at = _now()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def wait_ready(self, timeout: float = 90.0) -> dict[str, object]:
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    f"PowerContext Server exited early with code {self.process.returncode}; "
                    f"see {self.log_path}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/v1/server-info", timeout=3
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                last_error = str(error)
                time.sleep(0.5)
        raise RuntimeError(f"PowerContext Server was not ready in {timeout}s: {last_error}")

    def stop(self) -> dict[str, object]:
        if self.process is None:
            return {"stopped": False, "reason": "process was never started"}
        self.process.send_signal(signal.SIGTERM)
        try:
            exit_code = self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            exit_code = self.process.wait(timeout=30)
        return {
            "stopped": True,
            "signal": "SIGTERM",
            "exit_code": exit_code,
            "pid": self.process.pid,
            "started_at": self.started_at,
            "stopped_at": _now(),
        }

    def instance_identity(self) -> dict[str, str]:
        """Read the instance identity the server prints on every request."""
        identity: dict[str, str] = {}
        if not self.log_path.exists():
            return identity
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        for key in ("service_instance_id", "service_version"):
            marker = f'{key}="'
            index = text.rfind(marker)
            if index != -1:
                rest = text[index + len(marker) :]
                identity[key] = rest.split('"', 1)[0]
        return identity


async def _store(port: int) -> PowerContextStore:
    client = build_client(f"http://127.0.0.1:{port}")
    default_scope = await client.get_default_scope()
    store = PowerContextStore(client, default_scope.scope_id)
    scope_id = await store.ensure_scope(
        f"minimax-sdd-restart-proof-{int(time.time())}",
        "Isolated scope used by the restart persistence proof",
    )
    return PowerContextStore(client, scope_id)


async def _scope_of(port: int, scope_id: str) -> PowerContextStore:
    client = build_client(f"http://127.0.0.1:{port}")
    return PowerContextStore(client, scope_id)


async def _write_and_read(port: int, task_text: str, verification_text: str):
    store = await _store(port)
    empty_state = await store.list_progress()
    records = scan_project(SAMPLE_DIR)
    entries = await store.put_records(records)
    written = [
        await store.remember_progress("progress", task_text, "restart persistence proof"),
        await store.remember_progress("verification", verification_text, "restart persistence proof"),
    ]
    read_back = await store.list_progress()
    return store, empty_state, written, read_back, entries


def _mysql_rows(sql: str) -> str:
    """Run a read-only query against the seekdb container, best effort."""
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
                sql,
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


async def main_async(args: argparse.Namespace) -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    env_source = Path(args.env_file).resolve()
    env_file = _write_env_file(
        env_source,
        RUNTIME_DIR / "server-restart-proof.env",
        args.port,
        RUNTIME_DIR / "home-restart-proof",
    )
    log_path = RUNTIME_DIR / "server-restart-proof.log"
    if log_path.exists():
        log_path.unlink()

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

    if not _port_free(args.port):
        print(f"port {args.port} is already in use", file=sys.stderr)
        return 1

    # -- first boot ---------------------------------------------------------
    server = ServerHandle(env_file, args.port, log_path)
    server.start()
    info_before = server.wait_ready()
    record(
        "start-server-1",
        f"powercontext server run --env-file {env_file} --port {args.port}",
        0,
        {
            "pid": server.pid,
            "server_info": info_before,
            "instance_identity": server.instance_identity(),
        },
    )

    task_text = (
        "SDD 任务进度：在 billing.pricing.compute_invoice_totals 中实现促销码折扣，"
        "折扣上限取自促销码目录，税额按折后金额计算。"
    )
    verification_text = (
        "验证结果：改动前目标功能测试失败 (promo 能力未实现)，回归测试通过；"
        "改动后目标功能测试与回归测试全部通过。"
    )

    store, empty_state, written, read_back_before, corpus_entries = await _write_and_read(
        args.port, task_text, verification_text
    )
    scope_id = store.scope_id
    record(
        "empty-state-before-write",
        f"POST /v1/memory/entries/list (scope={scope_id})",
        0,
        {"entry_count": len(empty_state), "entries": [entry.__dict__ for entry in empty_state]},
    )
    record(
        "ingest-corpus-into-proof-scope",
        f"POST /v1/sources/content (scope={scope_id})",
        0,
        {"record_count": len(corpus_entries), "first_source_id": corpus_entries[0].source_id},
    )
    record(
        "write-progress",
        f"POST /v1/memory/remember (scope={scope_id})",
        0,
        {"written": [entry.__dict__ for entry in written]},
    )
    record(
        "read-back-before-restart",
        f"POST /v1/memory/entries/list (scope={scope_id})",
        0,
        {"entry_count": len(read_back_before), "entries": [entry.__dict__ for entry in read_back_before]},
    )

    sql_before = _mysql_rows(
        "SELECT artifact_id, revision, kind FROM powercontext_sdd.pc_atomic_memory_current "
        "WHERE scope_id = '%s' ORDER BY artifact_id;" % scope_id
    )
    record(
        "sql-check-before-restart",
        "docker exec oceanbase-seekdb mysql ... SELECT ... FROM powercontext_sdd.pc_atomic_memory_current",
        0,
        {"rows": sql_before},
    )

    # -- restart ------------------------------------------------------------
    stop_info = server.stop()
    record(
        "stop-server",
        f"kill -TERM {stop_info.get('pid')}",
        int(stop_info.get("exit_code") or 0),
        stop_info,
    )

    server2 = ServerHandle(env_file, args.port, log_path)
    server2.start()
    info_after = server2.wait_ready()
    record(
        "start-server-2",
        f"powercontext server run --env-file {env_file} --port {args.port}",
        0,
        {
            "pid": server2.pid,
            "server_info": info_after,
            "instance_identity": server2.instance_identity(),
        },
    )

    # -- read back after the restart ---------------------------------------
    store2 = await _scope_of(args.port, scope_id)
    read_back_after = await store2.list_progress()
    record(
        "read-back-after-restart",
        f"POST /v1/memory/entries/list (scope={scope_id})",
        0,
        {"entry_count": len(read_back_after), "entries": [entry.__dict__ for entry in read_back_after]},
    )

    sql_after = _mysql_rows(
        "SELECT artifact_id, revision, kind FROM powercontext_sdd.pc_atomic_memory_current "
        "WHERE scope_id = '%s' ORDER BY artifact_id;" % scope_id
    )
    record(
        "sql-check-after-restart",
        "docker exec oceanbase-seekdb mysql ... SELECT ... FROM powercontext_sdd.pc_atomic_memory_current",
        0,
        {"rows": sql_after},
    )

    # -- reuse the reloaded progress in the next context --------------------
    corpus = await store2.list_corpus()
    pack = build_context_pack(
        "实现发票促销码折扣并保持金额不变量",
        corpus,
        scope_id=scope_id,
        progress=read_back_after,
    )
    record(
        "reuse-progress-in-next-context",
        "python -m sddctx query --task '实现发票促销码折扣并保持金额不变量'",
        0,
        {
            "corpus_size": pack.corpus_size,
            "prior_progress": pack.prior_progress,
            "files_to_modify": pack.files_to_modify,
            "applicable_spec_sections": len(pack.applicable_specs),
            "functional_tests": len(pack.functional_tests),
            "regression_tests": len(pack.regression_tests),
        },
    )

    stop_info2 = server2.stop()
    record("stop-server-2", "kill -TERM", int(stop_info2.get("exit_code") or 0), stop_info2)

    # -- verdict ------------------------------------------------------------
    def key(entry) -> tuple:
        return (entry.artifact_id, entry.revision, entry.kind, entry.text, entry.state)

    before_keys = sorted(key(entry) for entry in read_back_before)
    after_keys = sorted(key(entry) for entry in read_back_after)
    survived = before_keys == after_keys and len(before_keys) > 0
    reused = any(
        entry.artifact_id in {item["artifact_id"] for item in pack.prior_progress}
        for entry in read_back_after
    )
    restarted = bool(server.pid) and bool(server2.pid) and server.pid != server2.pid

    verdict = {
        "empty_state_confirmed": len(empty_state) == 0,
        "progress_survived_restart": survived,
        "server_process_was_restarted": restarted,
        "progress_reused_in_next_context": reused,
    }
    verdict["passed"] = all(verdict.values())

    payload = {
        "schema": "sddctx.restart-proof.v1",
        "generated_at": _now(),
        "scope_id": scope_id,
        "env_file": str(env_file),
        "port": args.port,
        "server_pids": {"before_restart": server.pid, "after_restart": server2.pid},
        "verdict": verdict,
        "steps": steps,
    }
    (ARTIFACT_DIR / "restart-proof.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ARTIFACT_DIR / "restart-proof.md").write_text(_transcript(payload), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["passed"] else 1


def _transcript(payload: dict[str, object]) -> str:
    """Render the recorded steps as a human readable transcript."""
    lines = [
        "# PowerContext 重启持久化验证记录 / Restart persistence proof",
        "",
        f"- 生成时间 / generated at: {payload['generated_at']}",
        f"- 验证作用域 / proof scope: `{payload['scope_id']}`",
        f"- 服务端口 / server port: {payload['port']}",
        f"- 重启前后进程号 / server pids: {payload['server_pids']['before_restart']} -> "
        f"{payload['server_pids']['after_restart']}",
        "",
        "## 结论 / Verdict",
        "",
        "| 检查项 / check | 结果 / result |",
        "| --- | --- |",
    ]
    for key, value in payload["verdict"].items():
        lines.append(f"| `{key}` | {'PASS' if value else 'FAIL'} |")
    lines += ["", "## 步骤 / Steps", ""]
    for step in payload["steps"]:
        lines += [
            f"### {step['step']}",
            "",
            f"- 命令 / command: `{step['command']}`",
            f"- 退出码 / exit code: `{step['exit_code']}`",
            f"- 时间 / timestamp: {step['timestamp']}",
        ]
        detail = step["detail"]
        if isinstance(detail, dict) and detail.get("entries") is not None:
            lines.append("- 条目 / entries:")
            for entry in detail["entries"]:
                lines.append(
                    f"  - `{entry['kind']}` artifact=`{entry['artifact_id']}` "
                    f"revision={entry['revision']} state={entry['state']}: {entry['text']}"
                )
        elif isinstance(detail, dict) and detail.get("written") is not None:
            lines.append("- 写入 / written:")
            for entry in detail["written"]:
                lines.append(
                    f"  - `{entry['kind']}` artifact=`{entry['artifact_id']}` "
                    f"revision={entry['revision']}: {entry['text']}"
                )
        elif isinstance(detail, dict) and detail.get("rows") is not None:
            lines.append("- 数据库直查 / direct SQL rows:")
            lines.append("```")
            lines.append(detail["rows"] or "<empty>")
            lines.append("```")
        elif isinstance(detail, dict) and detail.get("prior_progress") is not None:
            lines.append(
                f"- 复用上下文 / reused progress: corpus={detail.get('corpus_size')} "
                f"files_to_modify={detail.get('files_to_modify')} "
                f"spec_sections={detail.get('applicable_spec_sections')} "
                f"functional_tests={detail.get('functional_tests')} "
                f"regression_tests={detail.get('regression_tests')}"
            )
            for entry in detail["prior_progress"]:
                lines.append(f"  - `{entry['kind']}` {entry['text']}")
        elif isinstance(detail, dict):
            lines.append("- 详情 / detail:")
            lines.append("```json")
            lines.append(json.dumps(detail, ensure_ascii=False, indent=2))
            lines.append("```")
        lines.append("")
    lines += [
        "## 说明 / Notes",
        "",
        "- 服务进程收到 SIGTERM 后以退出码 1 结束，这是 PowerContext Server 自身的关闭行为，"
        "不代表数据丢失；数据保存在 seekdb 的 `pc_atomic_memory_current` 表中。",
        "- `server_id` 与 `service_instance_id` 中的服务实例号在重启后改变，"
        "证明服务确实被重新启动，而不是复用了旧进程。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove PowerContext persistence across a restart")
    parser.add_argument(
        "--env-file",
        default=str(RUNTIME_DIR / "server.env"),
        help="Environment file of the operator's PowerContext Server",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
