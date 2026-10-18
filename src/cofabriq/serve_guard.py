"""The browser gate's trust boundary (0.5): who may change anything through
`cofabriq serve`.

The API is read-only today. Once a route can approve a plan or a test, a
click starts paid agent runs and writes worktrees, and any page open in the
same browser can send requests to localhost. So, before any such route
exists:

    every request    Host must be 127.0.0.1:<port> or localhost:<port>
                     (a site that points its own name at 127.0.0.1, DNS
                     rebinding, sends its own Host and is refused)
    every non-GET    Origin must be the UI's own origin (another tab's page
                     is refused), the body JSON (a plain HTML form can't
                     send it), and `Authorization: Bearer <token>` the
                     token made at start (compared in constant time)
    no token         every non-GET is refused: read-only

The token is printed once at start as a URL fragment (`/#token=...`): the
browser never sends the fragment to the server, so it is in no log, and
the UI keeps it in sessionStorage (gone with the tab).
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

SAFE = {"GET", "HEAD", "OPTIONS"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def guard(
    app: FastAPI, port: int, token: str | None, dev_origins: tuple[str, ...] = ()
) -> None:
    """Install the checks on `app`. `dev_origins`: extra origins allowed to
    write (the Vite dev server, e.g. http://127.0.0.1:5173)."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts} | set(dev_origins)

    def refuse(status: int, why: str) -> Response:
        return JSONResponse({"detail": why}, status_code=status)

    @app.middleware("http")
    async def check(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host", "") not in hosts:
            return refuse(403, "unexpected Host")
        if request.method in SAFE:
            return await call_next(request)
        if token is None:
            return refuse(405, "read-only: start with a write token to change anything")
        if request.headers.get("origin", "") not in origins:
            return refuse(403, "unexpected Origin")
        if not request.headers.get("content-type", "").startswith("application/json"):
            return refuse(415, "JSON only")
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(sent.encode(), token.encode()):
            return refuse(401, "bad or missing token")
        return await call_next(request)
