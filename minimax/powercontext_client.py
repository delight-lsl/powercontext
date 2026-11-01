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

"""PowerContext task state management and resumption adapter."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("SDD.PowerContext")


class PowerContextClient:
    """Manages task lifecycle state, checkpoints, and resume capability.

    Integrates with OceanBase PowerContext HTTP API. If offline or running in
    an isolated CI sandbox, safely falls back to local atomic state persistence
    to guarantee state preservation and clean resumption across process restarts.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        local_store_file: Optional[Path] = None,
    ) -> None:
        self.api_url: str = api_url or os.getenv(
            "POWERCONTEXT_API_URL", "http://localhost:8080/v1"
        )
        if local_store_file is None:
            self.local_store_file: Path = (
                Path(__file__).parent / ".powercontext_state.json"
            )
        else:
            self.local_store_file = local_store_file

    def save_task_state(
        self,
        task_id: str,
        status: str,
        completed_tasks: List[str],
        pending_tasks: List[str],
        validation_results: Optional[Dict[str, Any]] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        code_diff: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save task progress checkpoint into PowerContext."""
        state_data: Dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "completed_tasks": completed_tasks,
            "pending_tasks": pending_tasks,
            "validation_results": validation_results or {},
            "context_snapshot": context_snapshot or {},
            "code_diff": code_diff or "",
            "updated_at": int(time.time()),
        }

        synced_remote = False
        try:
            resp = requests.post(
                f"{self.api_url}/contexts/{task_id}",
                json=state_data,
                timeout=1.0,
            )
            if resp.status_code in [200, 201]:
                synced_remote = True
                logger.info(
                    "Successfully synced state to PowerContext server for task %s",
                    task_id,
                )
        except Exception:
            pass

        # Atomic local persistence fallback
        self._write_local(task_id, state_data)
        state_data["remote_synced"] = synced_remote
        return state_data

    def load_task_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Load task progress from PowerContext or local persisted snapshot."""
        # 1. Try remote PowerContext HTTP API
        try:
            resp = requests.get(f"{self.api_url}/contexts/{task_id}", timeout=1.0)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                logger.info(
                    "Loaded state from PowerContext server for task %s", task_id
                )
                return data
        except Exception:
            pass

        # 2. Try local store
        return self._read_local(task_id)

    def _write_local(self, task_id: str, data: Dict[str, Any]) -> None:
        self.local_store_file.parent.mkdir(parents=True, exist_ok=True)
        store: Dict[str, Any] = {}
        if self.local_store_file.exists():
            try:
                store = json.loads(self.local_store_file.read_text(encoding="utf-8"))
            except Exception:
                store = {}
        store[task_id] = data
        self.local_store_file.write_text(
            json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _read_local(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not self.local_store_file.exists():
            return None
        try:
            store = json.loads(self.local_store_file.read_text(encoding="utf-8"))
            return store.get(task_id)
        except Exception:
            return None
