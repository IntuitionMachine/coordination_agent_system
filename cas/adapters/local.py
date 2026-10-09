"""Local adapters: a sandboxed subprocess and an in-process callable (spec §6)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from cas.adapters.base import CapabilityDescriptor, PollResult, RunRequest


class LocalProcessAdapter:
    """Runs a configured command in a scratch directory with a minimal environment.

    The process gets the task as JSON (CAS_TASK_FILE) and writes `artifacts.json` into
    CAS_OUTPUT_DIR: a list of {type, artifact_id?, media_type?, schema_ref?, and one of
    content_text | content_json | path}. It receives no CAS credentials and no lease token;
    this adapter registers and submits on its behalf, so the process cannot write to CAS."""

    adapter_type = "local_process"

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> list[CapabilityDescriptor]:
        return []

    def start(self, request: RunRequest) -> str:
        command = request.config.get("command")
        if not command:
            raise RuntimeError("local_process adapter config needs a 'command' list")
        run_id = "run_" + uuid.uuid4().hex[:16]
        workdir = Path(tempfile.mkdtemp(prefix="cas-run-"))
        (workdir / "output").mkdir()
        (workdir / "task.json").write_text(json.dumps(
            {"contract": request.contract, "pinned_inputs": request.pinned_inputs,
             "allowed_scopes": sorted(request.policy_context.get("effective", []))}, default=str))
        env = {"PATH": os.environ.get("PATH", ""), "CAS_TASK_FILE": str(workdir / "task.json"),
               "CAS_OUTPUT_DIR": str(workdir / "output"), **{str(k): str(v) for k, v in request.config.get("env", {}).items()}}
        log = open(workdir / "run.log", "wb")
        proc = subprocess.Popen(command, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self._runs[run_id] = {"proc": proc, "workdir": workdir, "log": log,
                              "deadline": time.monotonic() + float(request.config.get("timeout_seconds", 300))}
        return run_id

    def poll(self, external_run_id: str) -> PollResult:
        run = self._runs.get(external_run_id)
        if run is None:
            return PollResult("error", "run is unknown to this worker (it may have restarted)", retryable=True)
        code = run["proc"].poll()
        if code is None:
            if time.monotonic() > run["deadline"]:
                self.cancel(external_run_id)
                return PollResult("error", "run exceeded its time limit", retryable=True)
            return PollResult("progress")
        if code != 0:
            return PollResult("error", f"process exited with status {code}", retryable=True)
        return PollResult("submitted")

    def cancel(self, external_run_id: str) -> bool:
        run = self._runs.get(external_run_id)
        if run is None:
            return False
        if run["proc"].poll() is None:
            run["proc"].kill()
        return True

    def collect(self, external_run_id: str) -> list[dict[str, Any]]:
        run = self._runs[external_run_id]
        out_dir: Path = run["workdir"] / "output"
        manifest = out_dir / "artifacts.json"
        if not manifest.exists():
            raise RuntimeError("the process finished without writing output/artifacts.json")
        refs = []
        for item in json.loads(manifest.read_text()):
            ref = {k: item[k] for k in ("artifact_id", "type", "media_type", "schema_ref", "content_text", "content_json")
                   if item.get(k) is not None}
            if item.get("path"):
                target = (out_dir / item["path"]).resolve()
                if out_dir.resolve() not in target.parents:
                    raise RuntimeError("artifact path escapes the output directory")
                ref["content_base64"] = base64.b64encode(target.read_bytes()).decode()
            refs.append(ref)
        return refs

    def cleanup(self, external_run_id: str) -> None:
        run = self._runs.pop(external_run_id, None)
        if run:
            run["log"].close()
            shutil.rmtree(run["workdir"], ignore_errors=True)


class InProcessAdapter:
    """Runs a registered Python callable on a thread. Used by the demo and the tests."""

    adapter_type = "in_process"

    def __init__(self) -> None:
        self._functions: dict[str, Callable[[RunRequest], list[dict[str, Any]]]] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    def register(self, name: str, fn: Callable[[RunRequest], list[dict[str, Any]]]) -> None:
        self._functions[name] = fn

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [CapabilityDescriptor(name) for name in self._functions]

    def start(self, request: RunRequest) -> str:
        fn = self._functions.get(request.config.get("function", ""))
        if fn is None:
            raise RuntimeError(f"no in-process function named '{request.config.get('function')}'")
        run_id = "run_" + uuid.uuid4().hex[:16]
        run: dict[str, Any] = {"done": False, "error": None, "artifacts": [], "cancelled": False}
        self._runs[run_id] = run

        def work() -> None:
            try:
                run["artifacts"] = fn(request)
            except Exception as exc:  # noqa: BLE001 - reported through poll()
                run["error"] = str(exc)
            run["done"] = True

        threading.Thread(target=work, daemon=True).start()
        return run_id

    def poll(self, external_run_id: str) -> PollResult:
        run = self._runs.get(external_run_id)
        if run is None:
            return PollResult("error", "run is unknown to this worker (it may have restarted)", retryable=True)
        if not run["done"]:
            return PollResult("progress")
        return PollResult("error", run["error"], retryable=True) if run["error"] else PollResult("submitted")

    def cancel(self, external_run_id: str) -> bool:
        run = self._runs.get(external_run_id)
        if run:
            run["cancelled"] = True
        return run is not None

    def collect(self, external_run_id: str) -> list[dict[str, Any]]:
        return self._runs[external_run_id]["artifacts"]

    def cleanup(self, external_run_id: str) -> None:
        self._runs.pop(external_run_id, None)
