"""The gallery's HTTP layer: a stdlib-threaded server (no web framework).

Routes::

    /                       the single-page gallery
    /static/<file>          css/js assets (path-traversal guarded)
    /api/health             version, db path, project root
    /api/summary            counts by provider / type / identity
    /api/facets             distinct filter values for the dropdowns
    /api/assets             ?q=&game=&character_id=&category=&asset_type=&perspective=&provider=&limit=&offset=
    /api/assets/<id>        one asset with its gate record, animation metadata + provenance
    /api/sequence           lightweight IDs/animation info for all current filtered results
    /api/identities         character identities
    /api/projects           GET list, POST create a gallery grouping
    /api/assets/<id>        PATCH tags, project, character identity and category
    /api/pipelines          every pipeline.json with its stage status + problems
    /api/rejections         the rejection log (grouped repeat offenders first)
    /api/providers          the recipe registry: providers, identity method, stages
    /img/<id>               the raw image bytes
    /thumb/<id>?size=128    a nearest-neighbour thumbnail (cached)

It serves only catalogued assets by id, so it cannot be walked as a general file
server. Pillow is imported lazily, only when a thumbnail is actually requested.
"""

from __future__ import annotations

import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .data import GalleryData

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
_THUMB_CACHE: dict[tuple[str, int], bytes] = {}


class GalleryRequestHandler(BaseHTTPRequestHandler):
    """One handler class shared by every server; the ``data`` facade is a class attribute."""

    server_version = "spritewarden-gallery/0.1"
    data: GalleryData

    # --- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def _trusted_host(self):
        host = self.headers.get('Host', '')
        port = self.server.server_address[1]
        return host in {f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}'}

    def do_POST(self) -> None:
        self._handle_mutation()

    def do_PATCH(self) -> None:
        self._handle_mutation()

    def _handle_mutation(self) -> None:
        import secrets
        from ..workflow_state import StateConflict
        if not self._trusted_host():
            return self._error(403, 'untrusted host')
        origin = self.headers.get('Origin')
        if origin != 'http://' + self.headers.get('Host', '') or self.headers.get('Sec-Fetch-Site', 'same-origin') not in {'same-origin', 'none'}:
            return self._error(403, 'same-origin request required')
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf_token):
            return self._error(403, 'invalid CSRF token')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self._error(415, 'application/json required')
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            return self._error(400, 'one Content-Length required')
        try:
            size = int(self.headers['Content-Length'])
        except (TypeError, ValueError):
            return self._error(400, 'invalid content length')
        if not 0 < size <= 32768:
            return self._error(413, 'body limit is 32768 bytes')
        try:
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
            parts = urlsplit(self.path)
            if parts.path == '/api/decisions' and self.command == 'POST':
                return self._json(self.data.decide(payload))
            if parts.path == '/api/projects' and self.command == 'POST':
                return self._json(self.data.create_project(payload), status=201)
            if parts.path.startswith('/api/assets/') and self.command == 'PATCH':
                asset_id = unquote(parts.path[len('/api/assets/'):])
                if not asset_id or '/' in asset_id:
                    return self._error(404, 'unknown asset')
                return self._json(self.data.update_asset_metadata(asset_id, payload))
            return self._error(404, 'unknown mutation endpoint')
        except StateConflict as exc:
            self._error(409, str(exc))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(503, str(exc))

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if not self._trusted_host():
            return self._error(403, 'untrusted host')
        try:
            self._route()
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - a viewer must not die on one bad request
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            except Exception:  # noqa: BLE001 - response already (partly) sent / client gone
                pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # --- response helpers --------------------------------------------------

    def _send(self, body: bytes, content_type: str, status: int = 200, *, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if not head and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status=status)

    # --- routing -----------------------------------------------------------

    def _route(self) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segments = [s for s in path.split("/") if s]

        if not segments:
            return self._static("index.html")
        head = segments[0]
        if head == 'review' and len(segments) == 1:
            return self._static('review.html')

        if head == "static" and len(segments) >= 2:
            return self._static("/".join(segments[1:]))
        if head == "api":
            return self._api(segments[1:], query)
        if head == "img" and len(segments) >= 2:
            return self._image(segments[1])
        if head == "thumb" and len(segments) >= 2:
            return self._thumbnail(segments[1], query)
        return self._error(404, f"no route for {path}")

    def _api(self, segments: list[str], query: dict[str, str]) -> None:
        data = self.data
        name = segments[0] if segments else ""
        if name == 'revision-image':
            path = data.revision_image(query.get('pipeline_id'), query.get('revision_id'), query.get('facing'))
            return self._send(path.read_bytes(), 'image/png')
        if name == 'compare':
            info, mask = data.compare(query.get('before'), query.get('after'))
            if query.get('mask') == '1':
                out = io.BytesIO(); mask.save(out, 'PNG')
                return self._send(out.getvalue(), 'image/png')
            return self._json(info)
        if name == 'session':
            return self._json({'csrf_token': self.server.csrf_token})
        if name == 'review':
            return self._json(data.review())
        if name == 'trace':
            return self._json(data.trace(query.get('run_id'), query.get('limit', 100), query.get('offset', 0)))
        if name == "health":
            return self._json(data.health())
        if name == "summary":
            return self._json(data.summary())
        if name == "facets":
            return self._json(data.facets())
        if name == "assets":
            if len(segments) >= 2:
                asset = data.asset(segments[1])
                if asset is None:
                    return self._error(404, f"asset {segments[1]!r} not found")
                return self._json(asset)
            return self._json(data.assets(**self._asset_filters(query)))
        if name == "sequence":
            filters = self._asset_filters(query)
            filters.pop("limit", None)
            filters.pop("offset", None)
            return self._json(data.asset_sequence(**filters))
        if name == "identities":
            return self._json(data.identities(query.get("game") or None))
        if name == "projects":
            return self._json(data.projects())
        if name == "pipelines":
            return self._json(data.pipelines())
        if name == "rejections":
            return self._json(data.rejections(query.get("provider") or None))
        if name == "providers":
            return self._json(data.providers())
        return self._error(404, f"unknown endpoint /api/{name}")

    @staticmethod
    def _asset_filters(query: dict[str, str]) -> dict[str, Any]:
        def _int(key: str, default: int) -> int:
            try:
                return int(query.get(key, default))
            except (TypeError, ValueError):
                return default

        return {
            "query": query.get("q", ""),
            "game": query.get("game") or None,
            "character_id": query.get("character_id") or None,
            "category": query.get("category") or None,
            "asset_type": query.get("asset_type") or None,
            "perspective": query.get("perspective") or None,
            "provider": query.get("provider") or None,
            "limit": _int("limit", 60),
            "offset": _int("offset", 0),
        }

    # --- files -------------------------------------------------------------

    def _static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._error(403, "path outside static dir")
        if not target.is_file():
            return self._error(404, f"{relative} not found")
        ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(target.read_bytes(), ctype)

    def _serve_asset(self, asset_id: str, *, thumb_size: int | None) -> None:
        asset = self.data.asset(asset_id)
        if asset is None or not asset.get("exists"):
            return self._error(404, f"asset {asset_id!r} has no file")
        path = Path(asset["resolved_path"])
        if thumb_size:
            key = (str(path), thumb_size)
            body = _THUMB_CACHE.get(key)
            if body is None:
                body = _make_thumbnail(path, thumb_size)
                if body is None:
                    return self._error(415, "not a readable image")
                _THUMB_CACHE[key] = body
            return self._send(body, "image/png")
        try:
            body = path.read_bytes()
        except OSError as exc:
            return self._error(500, str(exc))
        self._send(body, _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))

    def _image(self, asset_id: str) -> None:
        self._serve_asset(asset_id, thumb_size=None)

    def _thumbnail(self, asset_id: str, query: dict[str, str]) -> None:
        try:
            size = max(16, min(1024, int(query.get("size", 128))))
        except (TypeError, ValueError):
            size = 128
        self._serve_asset(asset_id, thumb_size=size)


def _make_thumbnail(path: Path, size: int) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((size, size), Image.NEAREST)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001 - any unreadable file is simply not thumbnailed
        return None


def make_server(data: GalleryData, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                *, quiet: bool = False) -> ThreadingHTTPServer:
    """A ready-to-serve ``ThreadingHTTPServer`` bound to ``(host, port)`` (port 0 = pick one)."""
    import secrets
    if host not in {'127.0.0.1', 'localhost'}:
        raise ValueError('review console must bind to loopback')
    handler = type("BoundGalleryHandler", (GalleryRequestHandler,), {"data": data})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.csrf_token = secrets.token_urlsafe(32)
    httpd.daemon_threads = True
    httpd.quiet = quiet  # type: ignore[attr-defined]
    return httpd


def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    project_root: str | Path | None = None,
    catalog: Any = None,
    open_browser: bool = False,
    quiet: bool = False,
) -> int:
    """Serve the gallery until Ctrl-C; returns a process exit code."""
    data = GalleryData(catalog=catalog, project_root=project_root)
    try:
        httpd = make_server(data, host, port, quiet=quiet)
    except OSError as exc:
        print(f"Could not bind {host}:{port}: {exc}")
        return 1
    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"
    print(f"sprite-warden gallery: {url}")
    print(f"  project: {data.project_root}")
    print(f"  catalog: {data.catalog.db_path}")
    print("  (Ctrl-C to stop)")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0
