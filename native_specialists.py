"""Keep local specialist MCP servers alive for one native worker process.

The MCP sessions are async and bound to their event loop. Native task tools are
synchronous, so this small bridge owns a dedicated loop thread. It starts only
when the task needs a specialist, and closes the child processes at worker exit.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import threading

_RUNTIME = None
_LOCK = threading.Lock()


class SpecialistRuntime:
    def __init__(self):
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, name="cale-specialists", daemon=True)
        self.loop = None
        self.shutdown = None
        self.error = None
        self.thread.start()

    def _thread_main(self):
        import toolbox_mcp

        async def serve():
            self.loop = asyncio.get_running_loop()
            self.shutdown = asyncio.Event()
            try:
                async with toolbox_mcp.lifespan(None):
                    self.ready.set()
                    await self.shutdown.wait()
            except BaseException as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.ready.set()

        asyncio.run(serve())

    def _submit(self, coroutine, timeout=120):
        if not self.ready.wait(timeout=60):
            raise TimeoutError("Local specialist tools did not start in 60 seconds")
        if self.error:
            raise RuntimeError(self.error)
        if not self.thread.is_alive() or self.loop is None:
            raise RuntimeError("Local specialist tool process stopped")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=timeout)

    def search(self, query: str):
        import toolbox_mcp

        # Read after the loop has finished populating the catalog.
        self._submit(asyncio.sleep(0))
        found = toolbox_mcp.toolbox_search(query)
        for item in found["tools"]:
            item["schema"] = toolbox_mcp.toolbox_schema(item["name"]).get("inputSchema", {})
        return found

    def catalog(self):
        """Return live specialist schemas for the rebuildable SQLite index."""
        import toolbox_mcp

        self._submit(asyncio.sleep(0))
        return {"tools": [
            {"name": name, "description": spec.get("description", ""),
             "schema": spec.get("inputSchema", {})}
            for name, spec in sorted(toolbox_mcp.CATALOG.items())
        ], "errors": toolbox_mcp.ERRORS.copy()}

    def call(self, tool: str, arguments: dict, project_folder: str = ""):
        import toolbox_mcp

        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        self._submit(asyncio.sleep(0))
        if tool not in toolbox_mcp.CATALOG and tool not in toolbox_mcp.LOCAL_CATALOG:
            raise ValueError("Discover an exact installed specialist tool first")
        value = self._submit(toolbox_mcp.toolbox_call(tool, json.dumps(arguments), project_folder),
                             timeout=1200 if tool.startswith("local.") else 180)
        return value.model_dump(by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value

    def close(self):
        if self.thread.is_alive() and self.loop is not None and self.shutdown is not None:
            self.loop.call_soon_threadsafe(self.shutdown.set)
            self.thread.join(timeout=15)


def runtime() -> SpecialistRuntime:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None or not _RUNTIME.thread.is_alive():
            _RUNTIME = SpecialistRuntime()
        return _RUNTIME


def close():
    global _RUNTIME
    with _LOCK:
        current = _RUNTIME
        _RUNTIME = None
    if current is not None:
        current.close()


atexit.register(close)
