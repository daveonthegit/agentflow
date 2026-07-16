"""Local read-only web UI over the observability projection.

Serves the observability projection (runs, work, evidence) as JSON and streams
live role transcripts and events via Server-Sent Events, tailing the same
evidence files the ``watch`` command follows. The surface is strictly read-only:

- It never writes workflow state and is never consulted as workflow authority;
  ``advance`` / ``approve`` / ``start`` derive Run State only from event replay
  in the run kernel. This module only reads through :mod:`agentflow.projection`.
- It exposes no approve/start/mutate route. Every non-GET method is refused, and
  the only GET routes are the projection, the per-Run stream, and the static
  page.

Path confinement (shared with the projection through
:func:`agentflow.projection.confined_run_dir` and
:func:`agentflow.projection.confined_file`):

- Run ids must be a single path component; ``.``/``..`` and any id with a path
  separator are rejected.
- A symlinked run directory whose real path escapes ``<data_dir>/runs/`` is
  omitted.
- An evidence or transcript symlink whose real path escapes its run directory is
  refused.
- A circular/self-referential evidence or transcript symlink is a confinement
  failure: that path is skipped without raising, so ``/api/projection`` and the
  SSE stream keep serving every sibling in-bounds run.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from typing import Callable, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from .projection import build_projection, confined_file, confined_run_dir

_STREAM_PREFIX = "/api/runs/"
_STREAM_SUFFIX = "/stream"


def _new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Complete lines appended to ``path`` past ``offset``, and the new offset.

    Byte-offset tracking mirrors the kernel's watch tail: only content up to the
    final newline is returned so a concurrently-growing file never yields a
    partial line. Any read error — including ``ELOOP`` from a circular symlink,
    or undecodable bytes — yields no lines and leaves the offset unchanged, so a
    hostile file is skipped rather than raised.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset
    complete = data[: last_newline + 1]
    try:
        text = complete.decode("utf-8")
    except UnicodeDecodeError:
        return [], offset
    lines = [line for line in text.split("\n") if line]
    return lines, offset + len(complete)


def _sse_frame(kind: str, source: str, line: str) -> str:
    payload = json.dumps(
        {"kind": kind, "source": source, "line": line}, sort_keys=True
    )
    return f"event: {kind}\ndata: {payload}\n\n"


def _collect_frames(run_dir: Path, offsets: dict[str, int]) -> list[str]:
    """SSE frames for new event and transcript lines since the last pass.

    Confined to ``run_dir``: events.jsonl and each ``*-transcript.jsonl`` are
    read only when their real path stays within the run directory. An escaping
    symlink is refused and a circular symlink is skipped, both without raising,
    so the stream keeps serving the run's other files.
    """
    sources: list[tuple[str, str]] = [("event", "events.jsonl")]
    try:
        transcript_names = sorted(
            path.name for path in run_dir.glob("*-transcript.jsonl")
        )
    except OSError:
        transcript_names = []
    for name in transcript_names:
        sources.append(("transcript", name))
    frames: list[str] = []
    for kind, filename in sources:
        path = confined_file(run_dir, filename)
        if path is None:
            continue
        lines, offsets[filename] = _new_lines(path, offsets.get(filename, 0))
        for line in lines:
            frames.append(_sse_frame(kind, filename, line))
    return frames


def iter_run_stream(
    run_dir: Path,
    *,
    follow: bool = True,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    should_continue: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Yield SSE frames for new event and transcript lines in ``run_dir``.

    With ``follow`` False the generator makes a single pass over current content
    and stops (used by one-shot callers and tests). Otherwise it polls until
    ``should_continue`` returns False (or forever when it is None), emitting new
    complete lines each pass. Strictly read-only and confinement-safe: a hostile
    evidence or transcript symlink is skipped, never raised.
    """
    offsets: dict[str, int] = {}
    while True:
        for frame in _collect_frames(run_dir, offsets):
            yield frame
        if not follow:
            return
        if should_continue is not None and not should_continue():
            return
        sleep(poll_interval)


def _stream_run_id(route: str) -> str | None:
    """Run id embedded in an ``/api/runs/<run_id>/stream`` route, or None."""
    if not route.startswith(_STREAM_PREFIX):
        return None
    rest = route[len(_STREAM_PREFIX):]
    if not rest.endswith(_STREAM_SUFFIX):
        return None
    encoded = rest[: -len(_STREAM_SUFFIX)]
    if not encoded:
        return None
    # Decode percent-escapes so an encoded ``..`` is caught by run-id
    # confinement rather than slipping through as opaque text.
    return unquote(encoded)


class _WebUIHandler(BaseHTTPRequestHandler):
    server_version = "AgentflowWebUI/1"

    # A read-only surface: no state is derived here and none is written.
    server: "AgentflowWebServer"  # narrow the attribute for readers

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_bytes(
                200, "text/html; charset=utf-8", _INDEX_HTML.encode("utf-8")
            )
            return
        if route == "/api/projection":
            self._send_projection()
            return
        run_id = _stream_run_id(route)
        if run_id is not None:
            follow = parse_qs(parsed.query).get("follow", ["1"]) != ["0"]
            self._stream(run_id, follow=follow)
            return
        self._send_bytes(404, "text/plain; charset=utf-8", b"not found\n")

    # Every mutating method is refused: the UI never approves, starts, or
    # otherwise mutates a Run. There is no route that could.
    def _refuse_mutation(self) -> None:
        self._send_bytes(
            405, "text/plain; charset=utf-8", b"read-only surface\n"
        )

    do_POST = _refuse_mutation  # noqa: N815
    do_PUT = _refuse_mutation  # noqa: N815
    do_DELETE = _refuse_mutation  # noqa: N815
    do_PATCH = _refuse_mutation  # noqa: N815

    def _send_projection(self) -> None:
        projection = build_projection(
            data_dir=self.server.data_dir,
            repository=self.server.repository,
        )
        body = json.dumps(projection, sort_keys=True).encode("utf-8")
        self._send_bytes(200, "application/json", body)

    def _stream(self, run_id: str, *, follow: bool) -> None:
        run_dir = confined_run_dir(self.server.data_dir / "runs", run_id)
        if run_dir is None:
            self._send_bytes(
                404, "text/plain; charset=utf-8", b"no such run\n"
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if not follow:
                for frame in iter_run_stream(run_dir, follow=False):
                    self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                return
            # Live tail: emit new frames each poll, plus an SSE comment
            # heartbeat so a disconnected client is detected promptly (the write
            # raises) rather than leaking a thread until the next real line.
            offsets: dict[str, int] = {}
            while self.server.is_serving():
                for frame in _collect_frames(run_dir, offsets):
                    self.wfile.write(frame.encode("utf-8"))
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(self.server.poll_interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *args: object) -> None:
        # Keep the CLI's stdout/stderr clean; the server prints only its URL.
        return


class AgentflowWebServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the read-only projection context.

    Binds on construction (pass port 0 for an ephemeral port and read the
    assigned port from ``server_address``). Streams run on daemon threads so the
    process never hangs on an open Server-Sent Events connection.
    """

    daemon_threads = True
    #: Seconds between live-tail polls of a Run's evidence files.
    poll_interval = 0.5

    def __init__(
        self,
        address: tuple[str, int],
        *,
        data_dir: Path,
        repository: Path,
    ) -> None:
        super().__init__(address, _WebUIHandler)
        self.data_dir = data_dir
        self.repository = repository
        self._serving = True

    def shutdown(self) -> None:
        # Signal live SSE loops to stop before the base server tears down.
        self._serving = False
        super().shutdown()

    def is_serving(self) -> bool:
        """False once ``shutdown`` was called, so live streams can stop."""
        return self._serving


def create_web_server(
    *,
    data_dir: Path,
    repository: Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> AgentflowWebServer:
    """Create (and bind) the read-only web server without serving requests.

    Callers invoke ``serve_forever`` to accept connections. Binds locally by
    default; port 0 selects a free port readable from ``server_address``.
    """
    return AgentflowWebServer(
        (host, port), data_dir=data_dir, repository=repository
    )


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentflow — read-only observability</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 1.5rem; }
  h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
  .note { color: #888; margin: 0 0 1.5rem; }
  section { margin-bottom: 2rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8884; vertical-align: top; }
  code { font-family: ui-monospace, monospace; }
  .run { border: 1px solid #8884; border-radius: 6px; margin-bottom: 1rem; }
  .run summary { cursor: pointer; padding: .5rem .75rem; font-weight: 600; }
  .stream { margin: 0; padding: .5rem .75rem; max-height: 20rem; overflow: auto;
            background: #8881; font-family: ui-monospace, monospace;
            font-size: 12px; white-space: pre-wrap; }
  .stream .transcript { display: block; }
  .stream .event { display: block; color: #6a9; }
  .empty { color: #888; font-style: italic; }
</style>
</head>
<body>
<h1>Agentflow observability</h1>
<p class="note">Read-only. This view never approves, starts, or mutates a Run;
it renders the observability projection and tails live role transcripts.</p>
<section><h2>Runs</h2><div id="runs"></div></section>
<section><h2>Work</h2><div id="work"></div></section>
<script>
function text(tag, value, cls) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = value == null ? "" : String(value);
  return el;
}
function renderWork(work) {
  const host = document.getElementById("work");
  host.textContent = "";
  const items = (work && work.items) || [];
  if (!items.length) { host.appendChild(text("p", "No work items.", "empty")); return; }
  const ready = new Set(((work && work.ready) || []).map(i => i.id));
  const done = new Set((work && work.completed_ids) || []);
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["id", "summary", "status"].forEach(h => head.appendChild(text("th", h)));
  table.appendChild(head);
  for (const item of items) {
    const row = document.createElement("tr");
    row.appendChild(text("td", item.id));
    row.appendChild(text("td", item.summary));
    const status = done.has(item.id) ? "completed" : ready.has(item.id) ? "ready" : "blocked";
    row.appendChild(text("td", status));
    table.appendChild(row);
  }
  host.appendChild(table);
}
function renderRuns(runs) {
  const host = document.getElementById("runs");
  host.textContent = "";
  if (!runs.length) { host.appendChild(text("p", "No runs yet.", "empty")); return; }
  for (const run of runs) {
    const box = document.createElement("details");
    box.className = "run";
    box.open = true;
    const summary = document.createElement("summary");
    summary.textContent = `${run.short_id}  ·  ${run.state}  ·  ${run.summary || ""}`;
    box.appendChild(summary);
    const stream = document.createElement("pre");
    stream.className = "stream";
    stream.appendChild(text("span", "… waiting for live output …", "empty"));
    box.appendChild(stream);
    host.appendChild(box);
    follow(run.run_id, stream);
  }
}
function follow(runId, stream) {
  let cleared = false;
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
  const append = kind => e => {
    if (!cleared) { stream.textContent = ""; cleared = true; }
    let line = e.data;
    try { line = JSON.parse(e.data).line; } catch (_) {}
    stream.appendChild(text("span", line, kind));
    stream.scrollTop = stream.scrollHeight;
  };
  source.addEventListener("transcript", append("transcript"));
  source.addEventListener("event", append("event"));
}
fetch("/api/projection").then(r => r.json()).then(p => {
  renderRuns(p.runs || []);
  renderWork(p.work || {});
}).catch(err => {
  document.getElementById("runs").appendChild(text("p", "Failed to load projection: " + err, "empty"));
});
</script>
</body>
</html>
"""
