from fastapi import FastAPI
from fastapi.testclient import TestClient

from cofabriq.serve_guard import guard

PORT = 8765
UI = f"http://127.0.0.1:{PORT}"


def app(token: str | None) -> TestClient:
    a = FastAPI()

    @a.get("/api/read")
    def read() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    @a.post("/api/approve")
    def approve() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"approved": True}

    guard(a, PORT, token, dev_origins=("http://127.0.0.1:5173",))
    return TestClient(a, base_url=UI)


def write(c: TestClient, **headers: str) -> int:
    base = {
        "origin": UI,
        "content-type": "application/json",
        "authorization": "Bearer t0k",
    }
    return c.post("/api/approve", json={}, headers=base | headers).status_code


def test_reads_need_only_the_right_host() -> None:
    c = app(None)
    assert c.get("/api/read").status_code == 200
    # DNS rebinding: evil.example resolves to 127.0.0.1, the browser sends its Host
    assert c.get("/api/read", headers={"host": "evil.example:8765"}).status_code == 403


def test_without_a_token_nothing_changes() -> None:
    assert write(app(None)) == 405


def test_a_write_needs_origin_json_and_the_token() -> None:
    c = app("t0k")
    assert write(c) == 200
    assert write(c, origin="http://127.0.0.1:5173") == 200  # the Vite dev server
    assert write(c, origin="https://evil.example") == 403  # another tab's page
    assert write(c, **{"content-type": "application/x-www-form-urlencoded"}) == 415
    assert write(c, authorization="Bearer wrong") == 401
    assert write(c, authorization="") == 401
    assert write(c, host="evil.example:8765") == 403
