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
                self.server.sleep(self.server.poll_interval)
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
    #: Injectable so tests can drive the SSE live-tail loop without real time.
    sleep: Callable[[float], None] = staticmethod(time.sleep)

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


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentflow</title>
<style>
  :root {
    color-scheme: dark;
    --accent: #F5B33D;
    --accent-press: #E09B22;
    --accent-tint: rgba(245,179,61,.16);
    --accent-tint-strong: rgba(245,179,61,.24);
    --text-on-accent: #1A1206;
    --glow-accent: 0 8px 26px -8px rgba(245,179,61,.5);
    --app-bg:
      radial-gradient(860px 520px at 11% -12%, rgba(245,179,61,.15), transparent 60%),
      radial-gradient(800px 580px at 100% 4%, rgba(96,150,245,.12), transparent 55%),
      radial-gradient(720px 640px at 66% 124%, rgba(245,179,61,.08), transparent 60%),
      linear-gradient(180deg, #121316 0%, #0C0D10 64%);
    --text: #ECECEF;
    --text-muted: #8C8C95;
    --text-faint: #5E5E66;
    --text-inverse: #141414;
    --hover: rgba(255,255,255,.05);
    --border-strong: rgba(255,255,255,.14);
    --glass-bg: rgba(255,255,255,.07);
    --glass-raised-bg: rgba(255,255,255,.10);
    --glass-border: rgba(255,255,255,.13);
    --glass-divider: rgba(255,255,255,.09);
    --glass-hover: rgba(255,255,255,.09);
    --glass-edge: inset 0 1px 0 rgba(255,255,255,.18);
    --glass-shadow: 0 20px 50px -15px rgba(0,0,0,.5);
    --success: #4AC06D;  --success-subtle: rgba(74,192,109,.14);
    --info: #5AA7ED;     --info-subtle: rgba(90,167,237,.14);
    --warning: #E0922F;  --warning-subtle: rgba(224,146,47,.14);
    --danger: #EF6B60;   --danger-subtle: rgba(239,107,96,.14);
    --font: "Inter", system-ui, sans-serif;
    --mono: ui-monospace, Menlo, monospace;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; height: 100vh; display: flex; overflow: hidden;
    background: var(--app-bg); color: var(--text);
    font: 400 14px/1.5 var(--font);
    font-variant-numeric: tabular-nums;
  }
  @keyframes af-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  .glass { background: var(--glass-bg); backdrop-filter: blur(24px) saturate(1.5); -webkit-backdrop-filter: blur(24px) saturate(1.5); }
  .glass-raised { background: var(--glass-raised-bg); backdrop-filter: blur(24px) saturate(1.5); -webkit-backdrop-filter: blur(24px) saturate(1.5); }
  .card { border: 1px solid var(--glass-border); box-shadow: var(--glass-shadow), var(--glass-edge); }
  .mono { font-family: var(--mono); }

  /* ---- rail ---- */
  .rail { width: 60px; flex: none; border-right: 1px solid var(--glass-divider); padding: 14px 0; display: flex; flex-direction: column; align-items: center; gap: 12px; }
  .logo { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); position: relative; flex: none; box-shadow: var(--glow-accent); }
  .logo::after { content: ""; position: absolute; right: 6px; bottom: 6px; width: 7px; height: 7px; border-radius: 2px; background: var(--text-on-accent); }
  .nav { width: 36px; height: 36px; border-radius: 9px; display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--text-muted); transition: background .15s; }
  .nav:hover { background: var(--hover); }
  .nav.active { background: var(--accent-tint); color: var(--accent); }

  /* ---- shell ---- */
  .main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .topbar { border-bottom: 1px solid var(--glass-divider); padding: 12px 20px; display: flex; align-items: center; gap: 14px; flex: none; }
  .topbar h1 { font: 500 16px var(--font); letter-spacing: -.01em; margin: 0; }
  .livepill { display: flex; align-items: center; gap: 6px; color: var(--text-faint); font: 400 12px var(--font); }
  .livedot { width: 6px; height: 6px; border-radius: 999px; background: var(--success); box-shadow: 0 0 0 3px var(--success-subtle); }
  .livedot.off { background: var(--danger); box-shadow: 0 0 0 3px var(--danger-subtle); }
  .spacer { flex: 1; }
  .search { display: flex; align-items: center; gap: 7px; padding: 6px 11px; border-radius: 6px; min-width: 200px; color: var(--text-faint); border: 1px solid var(--glass-border); }
  .search .q { flex: 1; outline: none; font: 400 13px var(--font); color: var(--text); min-width: 0; white-space: nowrap; overflow: hidden; }
  .search .q:empty::before { content: "Search runs…"; color: var(--text-faint); }
  .view { flex: 1; min-height: 0; display: none; }
  .view.active { display: flex; }
  .empty { color: var(--text-faint); font: 400 13px var(--font); padding: 24px; }

  /* ---- badges / chips ---- */
  .badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font: 500 11px var(--font); white-space: nowrap; }
  .badge-gate { background: var(--accent); color: var(--text-on-accent); }
  .badge-accent { background: var(--accent-tint); color: var(--accent); }
  .badge-success { background: var(--success-subtle); color: var(--success); }
  .badge-danger { background: var(--danger-subtle); color: var(--danger); }
  .badge-warning { background: var(--warning-subtle); color: var(--warning); }
  .badge-info { background: var(--info-subtle); color: var(--info); }
  .badge-muted { background: var(--hover); color: var(--text-muted); }
  .count-pill { font: 500 11px var(--mono); background: var(--hover); color: var(--text-faint); padding: 1px 8px; border-radius: 999px; }

  /* ---- runs view ---- */
  #view-runs { display: none; }
  #view-runs.active { display: grid; grid-template-columns: 340px 1fr; grid-template-rows: minmax(0, 1fr); }
  .runlist { border-right: 1px solid var(--glass-divider); display: flex; flex-direction: column; min-height: 0; }
  .runlist-head { padding: 11px 14px; border-bottom: 1px solid var(--glass-divider); display: flex; align-items: center; gap: 8px; font: 500 12px var(--font); color: var(--text-muted); }
  .runlist-head .count-pill { margin-left: auto; }
  .runlist-body { flex: 1; overflow: auto; padding: 9px 10px; display: flex; flex-direction: column; gap: 5px; }
  .runcard { padding: 11px 12px; border-radius: 9px; display: flex; flex-direction: column; gap: 8px; cursor: pointer; transition: background .15s; }
  .runcard:hover { background: var(--glass-hover); }
  .runcard.selected { background: var(--accent-tint); }
  .runcard .row { display: flex; align-items: center; gap: 8px; }
  .runcard .rid { font: 400 11px var(--mono); color: var(--text-faint); margin-left: auto; }
  .runcard .sum { font: 500 13px/1.3 var(--font); color: var(--text-muted);
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .runcard.selected .sum { color: var(--text); }
  .pips { display: flex; gap: 3px; }
  .pip { flex: 1; height: 4px; border-radius: 2px; background: var(--border-strong); }
  .pip-done { background: var(--accent); }
  .pip-success { background: var(--success); }
  .pip-danger { background: var(--danger); }
  .pip-warn { background: var(--warning); animation: af-pulse 1.6s infinite; }
  .pip-current { background: var(--accent-tint-strong); border: 1px solid var(--accent); animation: af-pulse 1.6s infinite; }

  .detail { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .detail-head { padding: 15px 20px; border-bottom: 1px solid var(--glass-divider); display: flex; align-items: flex-start; gap: 12px; flex: none; }
  .detail-head .t { font: 500 18px/1.2 var(--font); letter-spacing: -.01em; cursor: pointer;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .detail-head .t.expanded { display: block; -webkit-line-clamp: unset; max-height: 40vh;
    overflow: auto; font: 400 13px/1.5 var(--font); letter-spacing: 0;
    padding-right: 8px; overscroll-behavior: contain; }
  .detail-head .m { font: 400 12px var(--mono); color: var(--text-faint); margin-top: 4px; }
  .status-chip { display: inline-flex; align-items: center; gap: 7px; padding: 7px 13px; border-radius: 8px; font: 500 13px var(--font); flex: none; white-space: nowrap; }
  .gate-strip { display: none; padding: 11px 20px; border-bottom: 1px solid var(--glass-divider); background: var(--accent-tint); align-items: center; gap: 12px; flex: none; flex-wrap: wrap; }
  .gate-strip.on { display: flex; }
  .gate-strip .gt { font: 500 13px var(--font); color: var(--accent); }
  .gate-strip .gd { font: 400 12px var(--font); color: var(--text-muted); }
  .gate-cmd { font: 400 12px var(--mono); color: var(--text); background: rgba(0,0,0,.25); border: 1px solid var(--border-strong); border-radius: 7px; padding: 6px 11px; cursor: pointer; margin-left: auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 60%; transition: border-color .15s; }
  .gate-cmd:hover { border-color: var(--accent); }
  .detail-grid { display: grid; grid-template-columns: 1fr 272px; grid-template-rows: minmax(0, 1fr); flex: 1; min-height: 0; }

  .tpane { display: flex; flex-direction: column; border-right: 1px solid var(--glass-divider); min-width: 0; min-height: 0; }
  .tpane-head { padding: 9px 16px; border-bottom: 1px solid var(--glass-divider); display: flex; align-items: center; gap: 8px; font: 500 12px var(--font); color: var(--text-muted); }
  .tpane-head .src { margin-left: auto; color: var(--text-faint); font-weight: 400; }
  .transcript { flex: 1; padding: 14px 16px; overflow: auto; font: 400 12px/1.7 var(--mono); background: rgba(0,0,0,.14); }
  .transcript.live::after { content: "▍"; color: var(--accent); animation: af-pulse 1.6s infinite; }
  .tline { white-space: pre-wrap; word-break: break-word; }
  .tlabel { color: var(--text-faint); }
  .t-info { color: var(--info); }
  .t-role { color: var(--accent); }
  .t-success { color: var(--success); }
  .t-danger { color: var(--danger); }
  .t-warning { color: var(--warning); }
  .t-accent { color: var(--accent); }
  .t-faint { color: var(--text-faint); }
  .t-muted { color: var(--text-muted); }

  .rail-pane { padding: 15px; display: flex; flex-direction: column; gap: 17px; overflow: auto; min-height: 0; }
  .blk h3 { font: 500 12px var(--font); color: var(--text-muted); margin: 0 0 10px; }
  .stages { display: flex; flex-direction: column; gap: 9px; }
  .stage { display: flex; align-items: center; gap: 9px; font: 400 12px var(--font); color: var(--text-muted); }
  .stage .dot { width: 16px; height: 16px; border-radius: 999px; flex: none; background: var(--border-strong); color: var(--text-inverse); display: flex; align-items: center; justify-content: center; font: 500 10px var(--font); }
  .stage.done { color: var(--text); }
  .stage.done .dot { background: var(--accent); }
  .stage.approved .dot { background: var(--success); }
  .stage.current { color: var(--accent); font-weight: 500; }
  .stage.current .dot { background: transparent; border: 2px solid var(--accent); animation: af-pulse 1.6s infinite; }
  .stage.failed { color: var(--danger); font-weight: 500; }
  .stage.failed .dot { background: var(--danger); }
  .stage.warn { color: var(--warning); font-weight: 500; }
  .stage.warn .dot { background: var(--warning); }
  .kv { display: flex; flex-direction: column; gap: 7px; font: 400 12px var(--font); }
  .kv .r { display: flex; justify-content: space-between; gap: 8px; }
  .kv .k { color: var(--text-muted); }
  .acc { display: flex; flex-direction: column; gap: 7px; font: 400 12px/1.35 var(--font); }
  .acc .r { display: flex; gap: 7px; }
  .acc .mk { flex: none; }

  /* ---- work view ---- */
  #view-work.active { display: block; overflow: auto; padding: 22px 24px; }
  .work-inner { max-width: 1180px; display: flex; flex-direction: column; gap: 22px; }
  .work-intro { font: 400 13px/1.55 var(--font); color: var(--text-muted); margin: 0; max-width: 720px; }
  .grp-head { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
  .grp-dot { width: 7px; height: 7px; border-radius: 2px; }
  .grp-title { font: 500 13px var(--font); }
  .grp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
  .wcard { padding: 14px 15px; border-radius: 10px; display: flex; flex-direction: column; gap: 9px; }
  .wcard.inprog { box-shadow: var(--glass-shadow), var(--glass-edge), inset 0 2px 0 var(--accent); }
  .wcard .row { display: flex; align-items: center; gap: 8px; }
  .wcard .row .badge { margin-left: auto; }
  .wid { font: 500 11px var(--mono); color: var(--accent); background: var(--accent-tint); padding: 1px 7px; border-radius: 5px; }
  .wcard .sum { font: 500 14px/1.3 var(--font);
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .deps { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font: 400 11px var(--font); color: var(--text-faint); }
  .dep { font: 500 11px var(--mono); color: var(--text-muted); background: var(--hover); padding: 1px 6px; border-radius: 5px; }
  .dep.sat { color: var(--success); background: var(--success-subtle); }
  .wfoot { display: flex; align-items: center; gap: 10px; padding-top: 9px; border-top: 1px solid var(--glass-divider); font: 400 11px var(--font); color: var(--text-muted); }
  .wfoot .run { margin-left: auto; font: 400 11px var(--mono); }

  /* ---- evidence view ---- */
  #view-evidence.active { display: flex; flex-direction: column; }
  .ev-chips { padding: 11px 20px; border-bottom: 1px solid var(--glass-divider); display: flex; gap: 7px; overflow-x: auto; flex: none; }
  .ev-chip { padding: 6px 11px; border: 1px solid var(--border-strong); border-radius: 7px; color: var(--text-muted); font: 500 12px var(--font); white-space: nowrap; flex: none; cursor: pointer; transition: background .15s; }
  .ev-chip:hover { background: var(--hover); }
  .ev-chip.selected { border-color: var(--accent); background: var(--accent-tint); color: var(--accent); }
  .ev-body { flex: 1; overflow: auto; padding: 22px 24px; }
  .ev-inner { max-width: 1080px; display: flex; flex-direction: column; gap: 20px; }
  .ev-title { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .ev-title .t { font: 500 18px/1.3 var(--font); letter-spacing: -.01em; cursor: pointer; min-width: 0;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .ev-title .t.expanded { display: block; -webkit-line-clamp: unset; max-height: 40vh;
    overflow: auto; font: 400 13px/1.5 var(--font); letter-spacing: 0;
    padding-right: 8px; overscroll-behavior: contain; }
  .ev-title .id { font: 400 12px var(--mono); color: var(--text-faint); }
  .ev-title .ro { margin-left: auto; font: 400 12px var(--font); color: var(--text-faint); }
  .meta-card { border-radius: 10px; overflow: hidden; }
  .meta-row { display: grid; grid-template-columns: 190px 1fr; gap: 12px; padding: 11px 16px; border-bottom: 1px solid var(--glass-divider); }
  .meta-row:last-child { border-bottom: none; }
  .meta-row .k { font: 400 12px var(--font); color: var(--text-muted); }
  .meta-row .v { font: 400 13px var(--font); word-break: break-all; }
  .meta-row .v.mono { font: 400 13px var(--mono); }
  .log-title { font: 500 13px var(--font); margin: 0 2px 11px; }
  .log-card { border-radius: 10px; overflow: hidden; }
  .log-head, .log-row { display: grid; grid-template-columns: 96px 200px 1fr; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--glass-divider); align-items: center; }
  .log-row:last-child { border-bottom: none; }
  .log-head { padding: 9px 16px; font: 500 11px var(--font); color: var(--text-muted); }
  .log-row { font: 400 12px var(--mono); }
  .log-row .seq { color: var(--text-faint); }
  .log-row .det { color: var(--text-muted); word-break: break-word; }
</style>
</head>
<body>
<aside class="rail glass-raised">
  <span class="logo" title="Agentflow"></span>
  <div class="nav" id="nav-runs" role="button" tabindex="0" title="Runs">
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="5" y1="4" x2="14" y2="4"/><line x1="5" y1="8" x2="14" y2="8"/><line x1="5" y1="12" x2="14" y2="12"/><circle cx="2.4" cy="4" r="1" fill="currentColor" stroke="none"/><circle cx="2.4" cy="8" r="1" fill="currentColor" stroke="none"/><circle cx="2.4" cy="12" r="1" fill="currentColor" stroke="none"/></svg>
  </div>
  <div class="nav" id="nav-work" role="button" tabindex="0" title="Work graph">
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="4" cy="4" r="2"/><circle cx="4" cy="12" r="2"/><circle cx="12" cy="8" r="2"/><path d="M4 6v4M6 4h2a2 2 0 0 1 2 2M6 12h2a2 2 0 0 0 2-2"/></svg>
  </div>
  <div class="nav" id="nav-evidence" role="button" tabindex="0" title="Evidence">
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 2h5l3 3v9H4z"/><path d="M9 2v3h3"/><line x1="6" y1="9" x2="10" y2="9"/><line x1="6" y1="11.5" x2="10" y2="11.5"/></svg>
  </div>
</aside>

<div class="main">
  <header class="topbar glass-raised">
    <h1 id="view-title">Runs</h1>
    <div class="livepill"><span class="livedot" id="live-dot"></span><span id="live-text">live</span></div>
    <div class="spacer"></div>
    <div class="search glass">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg>
      <div class="q" id="search-q" contenteditable="plaintext-only" spellcheck="false"></div>
    </div>
  </header>

  <!-- Runs -->
  <div class="view" id="view-runs">
    <div class="runlist glass">
      <div class="runlist-head">Runs<span class="count-pill" id="run-count">0</span></div>
      <div class="runlist-body" id="run-cards"></div>
    </div>
    <main class="detail">
      <div class="detail-head">
        <div style="min-width:0;flex:1">
          <div class="t" id="detail-title">No runs yet</div>
          <div class="m" id="detail-meta">read-only observability — this surface never mutates a Run</div>
        </div>
        <span class="status-chip badge-muted" id="detail-status" hidden></span>
      </div>
      <div class="gate-strip" id="gate-strip">
        <span class="gt">Gate — awaiting your approval</span>
        <span class="gd" id="gate-detail"></span>
        <span class="gate-cmd" id="gate-cmd" role="button" tabindex="0" title="Click to copy"></span>
      </div>
      <div class="detail-grid" id="detail-grid">
        <div class="tpane">
          <div class="tpane-head"><span class="livedot"></span>Live transcript<span class="src">events.jsonl</span></div>
          <div class="transcript" id="transcript"></div>
        </div>
        <div class="rail-pane" id="evidence-rail"></div>
      </div>
    </main>
  </div>

  <!-- Work graph -->
  <div class="view" id="view-work">
    <div class="work-inner" id="work-inner">
      <p class="work-intro">The Work Graph is git-tracked in the target repo. Ready work is derived
      from dependencies and completion — a run delivers an item when its gate is approved. Nothing
      here is a mutable status; it is computed from evidence.</p>
      <div id="work-groups"></div>
    </div>
  </div>

  <!-- Evidence -->
  <div class="view" id="view-evidence">
    <div class="ev-chips" id="ev-chips"></div>
    <div class="ev-body">
      <div class="ev-inner" id="ev-inner"><div class="empty">No runs yet.</div></div>
    </div>
  </div>
</div>

<script>
"use strict";

/* ---------- state ---------- */
const S = {
  view: (location.hash.replace("#", "") || "runs"),
  sel: null,
  projection: null,
  query: "",
  es: null,
  esRun: null,
  evExpanded: false,
  evExpandedFor: null,
};
if (!["runs", "work", "evidence"].includes(S.view)) S.view = "runs";

/* ---------- helpers ---------- */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function shortSha(v) { return String(v).slice(0, 7); }
function truncate(s, n) {
  s = String(s).split(/\s+/).join(" ");
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}
function basename(p) { return String(p || "").split("/").filter(Boolean).pop() || String(p || ""); }

/* ---------- state model ---------- */
const STAGES = ["Build", "Verify", "Test", "Review", "Gate"];
const LIVE_STATES = new Set([
  "created", "ready", "planned", "built", "verified", "tested", "reviewed",
  "changes_requested", "tests_failed", "awaiting_human",
]);
const DONE_BY_STATE = {
  created: 0, ready: 0, planned: 0, built: 1, verified: 2, tested: 3,
  reviewed: 4, changes_requested: 3, tests_failed: 2, awaiting_human: 4,
  human_approved: 5, human_rejected: 4, unknown: 0,
};

function runEvents(run) {
  const entry = ((S.projection || {}).evidence || []).find(e => e.run_id === run.run_id);
  return entry ? entry.events : [];
}
function milestones(events) {
  const t = new Set(events.map(e => e.type));
  let done = 0;
  if (t.has("build_ready") || t.has("repair_ready")) done = 1;
  if (t.has("checks_passed")) done = 2;
  if (t.has("tests_ready")) done = 3;
  if (t.has("review_ready") || t.has("awaiting_human")) done = 4;
  if (t.has("human_approved")) done = 5;
  return done;
}
function pipeInfo(run) {
  const s = run.state;
  const info = {
    done: 0, failedAt: null, warnAt: null,
    live: LIVE_STATES.has(s),
    approved: s === "human_approved",
    rejected: s === "human_rejected",
    gate: s === "awaiting_human",
  };
  if (s === "plan_rejected") { info.done = 0; info.failedAt = 0; return info; }
  if (s in DONE_BY_STATE) {
    info.done = DONE_BY_STATE[s];
  } else {
    // failed / abandoned: progress from evidence milestones; the first
    // incomplete stage is where the run stopped.
    info.done = milestones(runEvents(run));
    if (s === "failed") info.failedAt = Math.min(info.done, 4);
  }
  if (s === "tests_failed") info.warnAt = 2;
  if (s === "changes_requested") info.warnAt = 3;
  return info;
}
function badgeOf(state) {
  const map = {
    awaiting_human: ["Gate", "badge-gate"],
    human_approved: ["Approved", "badge-success"],
    human_rejected: ["Rejected", "badge-danger"],
    failed: ["Failed", "badge-danger"],
    plan_rejected: ["Plan rejected", "badge-danger"],
    tests_failed: ["Tests failed", "badge-warning"],
    changes_requested: ["Changes requested", "badge-warning"],
    abandoned: ["Abandoned", "badge-muted"],
    created: ["Queued", "badge-muted"],
    ready: ["Queued", "badge-muted"],
    unknown: ["Unknown", "badge-muted"],
  };
  if (state in map) return { label: map[state][0], cls: map[state][1] };
  const label = state.charAt(0).toUpperCase() + state.slice(1).split("_").join(" ");
  return { label, cls: "badge-info" };
}

/* ---------- transcript / event formatting (mirrors agentflow watch) ---------- */
const EVENT_DETAIL_KEYS = [
  "candidate_sha", "approved_sha", "rejected_sha", "new_candidate_sha",
  "model", "approved_by", "abandoned_by", "rejected_by", "reason",
];
function eventDetail(ev) {
  const parts = [];
  for (const key of EVENT_DETAIL_KEYS) {
    const value = ev[key];
    if (value == null || value === "") continue;
    parts.push(key.endsWith("_sha") ? key + "=" + shortSha(value) : key + "=" + truncate(value, 60));
  }
  return parts.join("  ");
}
function fullEventDetail(ev) {
  const parts = [];
  for (const key of Object.keys(ev).sort()) {
    if (["run_id", "sequence", "type"].includes(key)) continue;
    let value = ev[key];
    if (value == null) continue;
    if (typeof value !== "string") value = JSON.stringify(value);
    parts.push(key + "=" + (key.endsWith("_sha") ? shortSha(value) : truncate(value, 80)));
  }
  return parts.join("  ");
}
function eventClass(type) {
  if (["checks_passed", "tests_ready", "review_ready", "human_approved"].includes(type)) return "t-success";
  if (["checks_failed", "tests_failed", "review_blocked", "repair_exhausted",
       "human_rejected", "plan_rejected", "run_abandoned"].includes(type)) return "t-danger";
  if (type === "awaiting_human") return "t-accent";
  return "t-info";
}
function displayShell(command) {
  let text = command.trim();
  for (const sep of [" && ", "\n"]) {
    const at = text.indexOf(sep);
    if (at >= 0) {
      const head = text.slice(0, at);
      const tail = text.slice(at + sep.length).trim();
      if (head.trimStart().startsWith("cd ") && tail) { text = tail; break; }
    }
  }
  return truncate(text, 90);
}
function toolSummary(name, input) {
  if (!input || typeof input !== "object") return name;
  if (["Bash", "Shell", "shell"].includes(name)) {
    const command = input.command;
    if (typeof command === "string" && command.trim()) return name + "  " + displayShell(command);
  }
  for (const key of ["file_path", "path", "target_notebook", "uri"]) {
    const value = input[key];
    if (typeof value === "string" && value) return name + "  " + basename(value);
  }
  return name;
}
function assistantTextBlocks(content) {
  if (typeof content === "string") { const t = content.trim(); return t ? [t] : []; }
  if (!Array.isArray(content)) return [];
  const out = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "text" && typeof block.text === "string" && block.text.trim()) {
      out.push(block.text.trim());
    } else if (block.type === "tool_use" && typeof block.name === "string" && block.name) {
      out.push("→ " + toolSummary(block.name, block.input));
    }
  }
  return out;
}
function formatTranscriptLine(label, line) {
  let payload;
  try { payload = JSON.parse(line); } catch (_) {
    const t = line.trim();
    return t ? [{ label, text: t, cls: "t-role" }] : [];
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return [];
  if (payload.type === "agentflow_adapter_attempt") {
    return payload.attempt != null ? [{ label, text: "attempt " + payload.attempt, cls: "t-faint" }] : [];
  }
  const type = payload.type, subtype = payload.subtype;
  if (["system", "rate_limit_event", "thinking", "content_block_delta"].includes(type)) return [];
  if (["delta", "thinking_tokens", "init"].includes(subtype)) return [];
  if (type === "assistant") {
    const message = payload.message;
    if (typeof message === "string" && message.trim()) return [{ label, text: message.trim(), cls: "t-role" }];
    const content = (message && typeof message === "object") ? message.content : payload.content;
    return assistantTextBlocks(content).map(text => ({
      label, text, cls: text.startsWith("→") ? "t-muted" : "t-role",
    }));
  }
  if (type === "tool_call" && subtype === "started") {
    const tool = payload.tool_call || payload.tool || {};
    const name = (tool && typeof tool === "object" && (tool.name || tool.toolName)) || payload.name;
    return (typeof name === "string" && name) ? [{ label, text: "→ " + name, cls: "t-muted" }] : [];
  }
  if (type === "tool_call" || type === "user") return [];
  if (type === "result") {
    return [{ label, text: "finished (" + (typeof subtype === "string" ? subtype : "done") + ")", cls: "t-faint" }];
  }
  return [];
}

/* ---------- live stream ---------- */
function openStream(runId) {
  if (S.es) { S.es.close(); S.es = null; }
  S.esRun = runId;
  const pane = document.getElementById("transcript");
  pane.textContent = "";
  if (!runId) return;
  const es = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/stream");
  S.es = es;
  // A reconnect replays the files from the start; clear so lines never double.
  es.addEventListener("open", () => { pane.textContent = ""; });
  es.addEventListener("event", m => appendFrame(m, "event"));
  es.addEventListener("transcript", m => appendFrame(m, "transcript"));
}
function appendFrame(message, kind) {
  let data;
  try { data = JSON.parse(message.data); } catch (_) { return; }
  let rendered = [];
  if (kind === "event") {
    let ev = null;
    try { ev = JSON.parse(data.line); } catch (_) {}
    if (!ev || typeof ev.type !== "string") return;
    if (["claim_acquired", "claim_released", "claim_expired"].includes(ev.type)) return;
    const detail = eventDetail(ev);
    rendered = [{
      label: "#" + (ev.sequence != null ? ev.sequence : "?"),
      text: "event  " + ev.type + (detail ? "  " + detail : ""),
      cls: eventClass(ev.type),
    }];
  } else {
    const label = String(data.source || "").replace(/-transcript\.jsonl$/, "");
    rendered = formatTranscriptLine(label, String(data.line || ""));
  }
  if (!rendered.length) return;
  const pane = document.getElementById("transcript");
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60;
  for (const r of rendered) {
    const line = el("div", "tline " + r.cls);
    line.appendChild(el("span", "tlabel", r.label));
    line.appendChild(document.createTextNode("  " + r.text));
    pane.appendChild(line);
  }
  if (nearBottom) pane.scrollTop = pane.scrollHeight;
}

/* ---------- runs view ---------- */
function sortedRuns() {
  const runs = (S.projection || {}).runs || [];
  const rank = r => r.state === "awaiting_human" ? 0 : LIVE_STATES.has(r.state) ? 1 : 2;
  return runs.slice().sort((a, b) => rank(a) - rank(b));
}
function selectedRun() {
  const runs = sortedRuns();
  return runs.find(r => r.run_id === S.sel) || runs[0] || null;
}
function renderPips(run) {
  const info = pipeInfo(run);
  const pips = el("div", "pips");
  for (let i = 0; i < 5; i++) {
    let cls = "pip";
    if (info.failedAt !== null) {
      if (i < info.failedAt) cls += " pip-done";
      else if (i === info.failedAt) cls += " pip-danger";
    } else if (info.rejected && i === 4) {
      cls += " pip-danger";
    } else if (info.warnAt !== null && i === info.warnAt) {
      cls += " pip-warn";
    } else if (i < info.done) {
      cls += (info.approved && i === 4) ? " pip-success" : " pip-done";
    } else if (i === info.done && info.live) {
      cls += " pip-current";
    }
    pips.appendChild(el("span", cls));
  }
  return pips;
}
function renderRunList() {
  const host = document.getElementById("run-cards");
  host.textContent = "";
  const query = S.query.toLowerCase();
  const runs = sortedRuns().filter(r =>
    !query
    || String(r.summary || "").toLowerCase().includes(query)
    || r.short_id.toLowerCase().includes(query)
    || String(r.state).toLowerCase().includes(query));
  document.getElementById("run-count").textContent = String(runs.length);
  if (!runs.length) {
    host.appendChild(el("div", "empty", S.query ? "No matching runs." : "No runs yet."));
    return;
  }
  const selected = selectedRun();
  for (const run of runs) {
    const card = el("div", "runcard" + (selected && run.run_id === selected.run_id ? " selected" : ""));
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    const row = el("div", "row");
    const badge = badgeOf(run.state);
    row.appendChild(el("span", "badge " + badge.cls, badge.label));
    row.appendChild(el("span", "rid", run.short_id));
    card.appendChild(row);
    const sum = el("div", "sum", run.summary || "(no summary)");
    sum.title = run.summary || "";
    card.appendChild(sum);
    card.appendChild(renderPips(run));
    card.addEventListener("click", () => { S.sel = run.run_id; render(); });
    host.appendChild(card);
  }
}
function renderStages(run) {
  const info = pipeInfo(run);
  const host = el("div", "stages");
  STAGES.forEach((name, i) => {
    let cls = "stage", mark = "", label = name;
    if (info.failedAt !== null && i === info.failedAt) { cls += " failed"; mark = "×"; }
    else if (info.rejected && i === 4) { cls += " failed"; mark = "×"; }
    else if (info.warnAt !== null && i === info.warnAt) { cls += " warn"; mark = "×"; }
    else if (i < info.done && !(info.failedAt !== null && i >= info.failedAt)) {
      cls += " done" + (info.approved && i === 4 ? " approved" : "");
      mark = "✓";
    } else if (i === info.done && info.live) {
      cls += " current";
      if (i === 4) label = "Gate — you";
    }
    const row = el("div", cls);
    row.appendChild(el("span", "dot", mark));
    row.appendChild(document.createTextNode(label));
    host.appendChild(row);
  });
  return host;
}
function lastEvent(events, types) {
  for (let i = events.length - 1; i >= 0; i--) {
    if (types.includes(events[i].type)) return events[i];
  }
  return null;
}
function renderChecks(run) {
  const events = runEvents(run);
  const host = el("div", "kv");
  const rows = [
    ["authoritative checks", lastEvent(events, ["checks_passed", "checks_failed"]), "checks_passed"],
    ["re-verify @ candidate", lastEvent(events, ["tests_ready", "tests_failed"]), "tests_ready"],
    ["reviewer eval", lastEvent(events, ["review_ready", "review_blocked"]), "review_ready"],
  ];
  for (const [name, ev, passType] of rows) {
    const row = el("div", "r");
    row.appendChild(el("span", "k", name));
    if (ev) {
      const ok = ev.type === passType;
      row.appendChild(el("span", ok ? "t-success" : "t-danger", ok ? "✓ passed" : "✗ failed"));
    } else {
      row.appendChild(el("span", "t-faint", "pending"));
    }
    host.appendChild(row);
  }
  return host;
}
function workItemFor(run) {
  if (!run.work_item_id) return null;
  const items = (((S.projection || {}).work) || {}).items || [];
  return items.find(item => item.id === run.work_item_id) || null;
}
function renderEvidenceRail(run) {
  const host = document.getElementById("evidence-rail");
  host.textContent = "";
  if (!run) return;

  const pipeline = el("div", "blk");
  pipeline.appendChild(el("h3", null, "Pipeline"));
  pipeline.appendChild(renderStages(run));
  host.appendChild(pipeline);

  const checks = el("div", "blk");
  checks.appendChild(el("h3", null, "Checks — authoritative"));
  checks.appendChild(renderChecks(run));
  host.appendChild(checks);

  const item = workItemFor(run);
  if (item && (item.acceptance_criteria || []).length) {
    const done = run.state === "human_approved";
    const block = el("div", "blk");
    block.appendChild(el("h3", null, "Acceptance criteria"));
    const list = el("div", "acc");
    for (const criterion of item.acceptance_criteria) {
      const row = el("div", "r");
      row.appendChild(el("span", "mk " + (done ? "t-success" : "t-faint"), done ? "✓" : "○"));
      row.appendChild(document.createTextNode(criterion));
      list.appendChild(row);
    }
    block.appendChild(list);
    host.appendChild(block);
  }

  if (run.evidence_truncated) {
    const warn = el("div", "blk");
    warn.appendChild(el("h3", null, "Evidence"));
    warn.appendChild(el("div", "t-warning", "events.jsonl is damaged — showing the intact prefix"));
    host.appendChild(warn);
  }
}
function renderDetail() {
  const run = selectedRun();
  const title = document.getElementById("detail-title");
  const meta = document.getElementById("detail-meta");
  const chip = document.getElementById("detail-status");
  const gate = document.getElementById("gate-strip");
  const pane = document.getElementById("transcript");
  if (!run) {
    title.textContent = "No runs yet";
    meta.textContent = "read-only observability — this surface never mutates a Run";
    chip.hidden = true;
    gate.classList.remove("on");
    pane.classList.remove("live");
    if (S.esRun) openStream(null);
    return;
  }
  title.textContent = run.summary || "(no summary)";
  title.title = "Click to expand";
  if (title.dataset.run !== run.run_id) {
    title.dataset.run = run.run_id;
    title.classList.remove("expanded");
  }
  meta.textContent = run.short_id
    + " · " + basename(run.repository)
    + (run.candidate_sha ? " · candidate " + shortSha(run.candidate_sha) : "")
    + (run.base_sha ? " ← base " + shortSha(run.base_sha) : "");
  const info = pipeInfo(run);
  const badge = badgeOf(run.state);
  chip.hidden = false;
  chip.className = "status-chip " + badge.cls;
  chip.textContent = run.state === "human_approved" && run.approved_sha
    ? "Approved · " + shortSha(run.approved_sha)
    : badge.label;
  if (info.gate) {
    gate.classList.add("on");
    document.getElementById("gate-detail").textContent = run.candidate_sha
      ? "bound to candidate " + shortSha(run.candidate_sha) : "";
    const cmd = document.getElementById("gate-cmd");
    cmd.textContent = "agentflow approve " + run.run_id + " --approved-by <you>";
    cmd.title = "Click to copy — the gate is a deliberate human command";
  } else {
    gate.classList.remove("on");
  }
  pane.classList.toggle("live", info.live);
  if (S.esRun !== run.run_id) openStream(run.run_id);
  renderEvidenceRail(run);
}

/* ---------- work view ---------- */
function renderWork() {
  const host = document.getElementById("work-groups");
  host.textContent = "";
  const work = ((S.projection || {}).work) || { items: [], ready: [], completed_ids: [] };
  const items = work.items || [];
  if (!items.length) {
    host.appendChild(el("div", "empty", "No work items — the Work Graph in this repository is empty."));
    return;
  }
  const done = new Set(work.completed_ids || []);
  const readyIds = new Set((work.ready || []).map(item => item.id));
  const runByItem = {};
  for (const run of (S.projection.runs || [])) {
    const id = run.work_item_id;
    if (!id) continue;
    const prev = runByItem[id];
    if (!prev || LIVE_STATES.has(run.state)
        || (!LIVE_STATES.has(prev.state) && run.state === "human_approved")) {
      runByItem[id] = run;
    }
  }
  const ACTIVE = new Set(["planned", "built", "verified", "tested", "reviewed",
                          "changes_requested", "tests_failed", "awaiting_human"]);
  const statusOf = item => {
    if (done.has(item.id)) return "done";
    const run = runByItem[item.id];
    if (run && ACTIVE.has(run.state)) return "in_progress";
    if (readyIds.has(item.id)) return "ready";
    return "blocked";
  };
  const groups = [
    { key: "done", title: "Done", dot: "var(--success)", badge: ["Done", "badge-success"] },
    { key: "in_progress", title: "In progress", dot: "var(--accent)", badge: ["In progress", "badge-accent"] },
    { key: "ready", title: "Ready", dot: "var(--info)", badge: ["Ready", "badge-info"] },
    { key: "blocked", title: "Blocked", dot: "var(--text-faint)", badge: ["Blocked", "badge-muted"] },
  ];
  for (const group of groups) {
    const members = items.filter(item => statusOf(item) === group.key);
    if (!members.length) continue;
    const section = el("div");
    section.style.marginBottom = "22px";
    const head = el("div", "grp-head");
    const dot = el("span", "grp-dot");
    dot.style.background = group.dot;
    head.appendChild(dot);
    head.appendChild(el("span", "grp-title", group.title));
    head.appendChild(el("span", "count-pill", String(members.length)));
    section.appendChild(head);
    const grid = el("div", "grp-grid");
    for (const item of members) {
      const card = el("div", "wcard glass card" + (group.key === "in_progress" ? " inprog" : ""));
      const row = el("div", "row");
      row.appendChild(el("span", "wid", item.id));
      row.appendChild(el("span", "badge " + group.badge[1], group.badge[0]));
      card.appendChild(row);
      const wsum = el("div", "sum", item.summary || "(no summary)");
      wsum.title = item.summary || "";
      card.appendChild(wsum);
      const deps = el("div", "deps");
      const dependsOn = item.depends_on || [];
      deps.appendChild(el("span", null, dependsOn.length ? "Depends on" : "No dependencies"));
      for (const dep of dependsOn) {
        deps.appendChild(el("span", "dep" + (done.has(dep) ? " sat" : ""), dep));
      }
      card.appendChild(deps);
      const foot = el("div", "wfoot");
      const criteria = (item.acceptance_criteria || []).length;
      foot.appendChild(el("span", null, criteria + " acceptance criteria"));
      const run = runByItem[item.id];
      const runLabel = el("span", "run", run
        ? run.short_id + " · " + badgeOf(run.state).label.toLowerCase()
        : "no run");
      runLabel.style.color = run ? "var(--text-muted)" : "var(--text-faint)";
      foot.appendChild(runLabel);
      card.appendChild(foot);
      card.addEventListener("click", () => {
        if (!run) return;
        S.sel = run.run_id;
        setView("runs");
      });
      if (run) { card.style.cursor = "pointer"; card.title = "Open run " + run.short_id; }
      grid.appendChild(card);
    }
    section.appendChild(grid);
    host.appendChild(section);
  }
}

/* ---------- evidence view ---------- */
function metaRow(key, value, mono, color) {
  const row = el("div", "meta-row");
  row.appendChild(el("span", "k", key));
  const v = el("span", "v" + (mono ? " mono" : ""), value);
  if (color) v.style.color = color;
  row.appendChild(v);
  return row;
}
function renderEvidence() {
  const chips = document.getElementById("ev-chips");
  const inner = document.getElementById("ev-inner");
  chips.textContent = "";
  inner.textContent = "";
  const runs = sortedRuns();
  if (!runs.length) {
    inner.appendChild(el("div", "empty", "No runs yet."));
    return;
  }
  const selected = selectedRun();
  for (const run of runs) {
    const chip = el("span", "ev-chip" + (run.run_id === selected.run_id ? " selected" : ""), run.short_id);
    chip.setAttribute("role", "button");
    chip.tabIndex = 0;
    chip.addEventListener("click", () => { S.sel = run.run_id; render(); });
    chips.appendChild(chip);
  }
  const run = selected;

  if (S.evExpandedFor !== run.run_id) S.evExpanded = false;
  const title = el("div", "ev-title");
  const evSummary = el("span", "t" + (S.evExpanded ? " expanded" : ""), run.summary || "(no summary)");
  evSummary.title = "Click to expand";
  evSummary.addEventListener("click", () => {
    S.evExpanded = !S.evExpanded;
    S.evExpandedFor = run.run_id;
    render();
  });
  title.appendChild(evSummary);
  title.appendChild(el("span", "id", run.run_id));
  title.appendChild(el("span", "ro", "Read-only · rebuildable from events"));
  inner.appendChild(title);

  const card = el("div", "meta-card glass card");
  card.appendChild(metaRow("State", badgeOf(run.state).label, false));
  if (run.repository) card.appendChild(metaRow("Repository", run.repository, true));
  if (run.base_sha) card.appendChild(metaRow("Base SHA", run.base_sha, true));
  card.appendChild(metaRow("Candidate SHA", run.candidate_sha || "—", true,
    run.candidate_sha ? "var(--accent)" : "var(--text-faint)"));
  if (run.approved_sha) card.appendChild(metaRow("Approved SHA", run.approved_sha, true, "var(--success)"));
  if (run.worktree) card.appendChild(metaRow("Worktree", run.worktree, true));
  if (run.work_item_id) card.appendChild(metaRow("Work item", run.work_item_id, true));
  if (run.evidence_truncated) {
    card.appendChild(metaRow("Evidence", "truncated — corrupt tail ignored", false, "var(--warning)"));
  }
  inner.appendChild(card);

  const logWrap = el("div");
  logWrap.appendChild(el("div", "log-title", "Event log — events.jsonl"));
  const log = el("div", "log-card glass card");
  const head = el("div", "log-head");
  head.appendChild(el("span", null, "Seq"));
  head.appendChild(el("span", null, "Type"));
  head.appendChild(el("span", null, "Detail"));
  log.appendChild(head);
  const events = runEvents(run);
  if (!events.length) {
    const row = el("div", "log-row");
    row.appendChild(el("span", "seq", "—"));
    row.appendChild(el("span", "t-faint", "no events"));
    row.appendChild(el("span", "det", ""));
    log.appendChild(row);
  }
  for (const ev of events) {
    const type = typeof ev.type === "string" ? ev.type : "(invalid)";
    const row = el("div", "log-row");
    row.appendChild(el("span", "seq", "#" + (ev.sequence != null ? ev.sequence : "?")));
    const isClaim = ["claim_acquired", "claim_released", "claim_expired"].includes(type);
    row.appendChild(el("span", isClaim ? "t-faint" : eventClass(type), type));
    row.appendChild(el("span", "det", fullEventDetail(ev)));
    log.appendChild(row);
  }
  logWrap.appendChild(log);
  inner.appendChild(logWrap);
}

/* ---------- shell ---------- */
function setView(view) {
  S.view = view;
  if (("#" + view) !== location.hash) location.hash = view;
  render();
}
function render() {
  const titles = { runs: "Runs", work: "Work graph", evidence: "Evidence" };
  document.getElementById("view-title").textContent = titles[S.view];
  for (const view of ["runs", "work", "evidence"]) {
    document.getElementById("nav-" + view).classList.toggle("active", S.view === view);
    document.getElementById("view-" + view).classList.toggle("active", S.view === view);
  }
  if (!S.projection) return;
  const selected = selectedRun();
  S.sel = selected ? selected.run_id : null;
  renderRunList();
  renderDetail();
  renderWork();
  renderEvidence();
}
function setLive(ok) {
  document.getElementById("live-dot").classList.toggle("off", !ok);
  document.getElementById("live-text").textContent = ok ? "live" : "offline";
}
async function refresh() {
  try {
    const response = await fetch("/api/projection");
    if (!response.ok) throw new Error(String(response.status));
    S.projection = await response.json();
    setLive(true);
  } catch (_) {
    setLive(false);
    return;
  }
  render();
}

document.getElementById("detail-title").addEventListener("click", event => {
  event.currentTarget.classList.toggle("expanded");
});
document.getElementById("nav-runs").addEventListener("click", () => setView("runs"));
document.getElementById("nav-work").addEventListener("click", () => setView("work"));
document.getElementById("nav-evidence").addEventListener("click", () => setView("evidence"));
window.addEventListener("hashchange", () => {
  const view = location.hash.replace("#", "");
  if (["runs", "work", "evidence"].includes(view) && view !== S.view) { S.view = view; render(); }
});
document.addEventListener("keydown", event => {
  if (event.key !== "Enter") return;
  const target = event.target;
  if (target instanceof HTMLElement && target.getAttribute("role") === "button") {
    event.preventDefault();
    target.click();
  }
});
document.getElementById("search-q").addEventListener("input", event => {
  S.query = event.target.textContent.trim();
  renderRunList();
});
document.getElementById("gate-cmd").addEventListener("click", event => {
  const node = event.currentTarget;
  const command = node.textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(command).then(() => {
      node.style.borderColor = "var(--success)";
      setTimeout(() => { node.style.borderColor = ""; }, 900);
    }).catch(() => {});
  }
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
