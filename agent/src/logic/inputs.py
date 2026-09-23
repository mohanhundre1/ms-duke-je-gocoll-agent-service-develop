    Transport (the A2A executor) drops the raw DataPart(s) and ``io="input"`` file
    parts into ``ExecutionContext.request.input_data``; these helpers turn that into
    a typed :class:`GoCollInputs` and materialize every referenced document to a
    local path via the injected file service. Nothing here imports A2A or the
    framework runtime, so the logic stays unit-testable with a fake file service.
    """

    from __future__ import annotations

    import asyncio
    import base64
    import logging
    import os
    import tempfile
    import uuid
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Any

    logger = logging.getLogger(__name__)
    ```
*   **Dataclass Definition (Image 5 & 4: L25-50):**
    ```python
    @dataclass
    class GoCollInputs:
        """Parsed GOCOLL request payload (transport-neutral)."""

        dp: dict | None
        input_files: list[dict]
        run_id: str
        tenant_id: str
        user_id: str | None
        period_date: Any
        period_label: str | None
        go_form_paths: Any | None
        source_files: list[str] = field(default_factory=list)
        workbook_path: str | None = None
        source_file_contents: list[dict] = field(default_factory=list)
        #: A2A ``context_id`` (conversation id) - used to recover prior-turn uploads
        #: from the Conversation Manager when a turn arrives with no inline input.
        conversation_id: str = ""
        #: File-Manager output target URL (the ``io="output"`` part), echoed onto the
        #: result so downstream steps resolve it without re-fetching.
        fm_output_url: str = ""

        @property
        def has_input(self) -> bool:
            """True when either a usable DataPart or an ``io="input"`` file is present."""
            return self.dp is not None or bool(self.input_files)
    ```
    *Self-correction:* I need to carefully check the line breaks and indentation of `has_input`. The first image shows `return self.dp is not None or bool(self.input_files)`. It seems `has_input` is a property.

*   **`parse_inputs` function (Image 3 & 4: L53-113):**
    ```python
    def parse_inputs(request: Any) -> GoCollInputs:
        """Build :class:`GoCollInputs` from ``request.input_data`` (set by the executor).

        ``run_id`` precedence matches the legacy executor: ``pipeline_run_id`` >
        ``run_id`` > ``job_id`` > the transport-provided task id.
        """
        data = request.input_data or {}
        data_parts: list[dict] = list(data.get("data_parts") or [])
        input_files: list[dict] = list(data.get("input_files") or [])
        # Tenant + task id come from the framework-built context (header > security
        # context, and the executor's execution_id); fall back to the DataPart for
        # older callers / direct unit tests.
        security = getattr(request, "security", None)
        tenant_id = str(getattr(security, "tenant_id", "") or data.get("tenant_id") or "")
        task_id = str(data.get("task_id") or getattr(request, "execution_id", "") or "")

        dp = next(
            (
                d
                for d in data_parts
                if isinstance(d, dict)
                and (
                    d.get("source_files")
                    or d.get("workbook_path")
                    or d.get("source_file_contents")
                )
            ),
            None,
        )
        d = dp or {}
        run_id = str(
            d.get("pipeline_run_id")
            or d.get("run_id")
            or d.get("job_id")
            or task_id
            or uuid.uuid4()
        )
        conversation_id = str(getattr(request, "conversation_id", "") or "")
        # The FM output target rides an ``io="output"`` file part (surfaced by the
        # framework under ``output_files``); take the first that carries a URL.
        fm_output_url = ""
        for _of in data.get("output_files") or []:
            _u = str((_of or {}).get("url") or "").strip()
            if _u:
                fm_output_url = _u
                break
        return GoCollInputs(
            dp=dp,
            input_files=input_files,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=d.get("user_id"),
            period_date=d.get("period_date"),
            period_label=d.get("period_label"),
            go_form_paths=d.get("go_form_paths"),
            source_files=list(d.get("source_files") or []),
            workbook_path=(str(d["workbook_path"]) if d.get("workbook_path") else None),
            source_file_contents=list(d.get("source_file_contents") or []),
            conversation_id=conversation_id,
            fm_output_url=fm_output_url,
        )
    ```
    *Self-correction:* Note the line breaks in the `next()` generator expression. The code reads:
    ```python
    dp = next(
        (
            d
            for d in data_parts
            if isinstance(d, dict)
            and (
                d.get("source_files")
                or d.get("workbook_path")
                or d.get("source_file_contents")
            )
        ),
        None,
    )
    ```
    The `return GoCollInputs(...)` is split across lines 100-113.

*   **`resolve_source_paths` function (Image 3 & 2: L116-129):**
    ```python
    async def resolve_source_paths(files: Any, source_paths: list[str]) -> list[str]:
        """Materialize any non-local source file (blob URI / cache path) via storage."""
        resolved: list[str] = []
        for sp in source_paths:
            if Path(sp).exists():
                resolved.append(sp)
                continue
            try:
                local = await asyncio.to_thread(files.ensure_local, sp)
                resolved.append(str(local))
            except Exception as exc:  # noqa: BLE001 - logged, keep original ref
                logger.warning("Failed to resolve source file %s: %s", sp, exc)
                resolved.append(sp)
        return resolved
    ```

*   **`resolve_input_file_parts` function (Image 2 & 1: L132-173):**
    ```python
    async def resolve_input_file_parts(files: Any, input_files: list[dict]) -> list[str]:
        """Download ``io="input"`` URL file parts, preserving each original filename.

        GOCOLL classifies feeds by filename (batch PDF / Validation Tab / Treasury /
        MF), so the local copy must keep the source name and extension.
        """
        try:
            worker_count = max(
                1,
                min(
                    int(os.getenv("GOCOLL_FILE_DOWNLOAD_CONCURRENCY", "8")),
                    len(input_files) or 1,
                ),
            )
        except (TypeError, ValueError):
            worker_count = min(8, len(input_files) or 1)
        semaphore = asyncio.Semaphore(worker_count)

        async def _resolve(f: dict) -> str | None:
            url = str(f.get("url") or "").strip()
            if not url:
                return None
            if Path(url).exists():
                return url
            name = str(f.get("name") or f.get("filename") or "").strip()
            hint = name if "." in name else ""
            try:
                async with semaphore:
                    local = await asyncio.to_thread(
                        files.ensure_local,
                        url,
                        filename_hint=hint,
                    )
                logger.info("GOCOLL: downloaded input file %s -> %s", name or url, local)
                return str(local)
            except Exception as exc:  # noqa: BLE001 - logged, keep original ref
                logger.warning("Failed to resolve input file %s: %s", url, exc)
                return url

        resolved = await asyncio.gather(*(_resolve(f) for f in input_files))
        return [path for path in resolved if path is not None]
    ```

*   **`materialize_base64` function (Image 2 & 1: L175-190):**
    ```python
    def materialize_base64(source_file_contents: list[dict]) -> list[str]:
        """Write any inline base64 source payloads to temp files, return their paths."""
        paths: list[str] = []
        for fc in source_file_contents or []:
            if not isinstance(fc, dict):
                continue
            b64 = fc.get("content_b64", "")
            if not b64:
                continue
            fname = fc.get("filename") or "upload.bin"
            tmp_path = Path(tempfile.mkdtemp(prefix="gocoll_agent_")) / fname
            tmp_path.write_bytes(base64.b64decode(b64))
            logger.info("GOCOLL: materialized base64 source file %s", tmp_path)
            paths.append(str(tmp_path))
        return paths
    ```