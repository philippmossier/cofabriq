from pathlib import Path

from cofabriq.loop.sentinel import (
    Sentinel,
    allowed_hosts,
    check,
    checker,
    load_sentinel,
)

CSP = """export const CSP = [
  "default-src 'self'",
  "connect-src 'self' https://huggingface.co https://*.huggingface.co https://*.hf.co",
];"""
SENTINEL = Sentinel(name="privacy", policy_file="vite.config.ts")
TESTS = ["src/**/*.test.ts", "e2e/**/*.spec.ts"]


def diff(path: str, *added: str) -> str:
    lines = "\n".join(f"+{a}" for a in added)
    head = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@"
    return f"{head}\n{lines}\n"


def hosts_flagged(d: str) -> list[str]:
    return [v.host or v.kind for v in check(d, allowed_hosts(CSP), SENTINEL, TESTS)]


def test_the_allowed_hosts_come_from_the_policy() -> None:
    assert allowed_hosts(CSP) == {"huggingface.co", "*.huggingface.co", "*.hf.co"}


def test_leaks_are_caught() -> None:
    telemetry = diff(
        "src/lib/chat/store.ts",
        '  void fetch("https://telemetry.example.com/e", { method: "POST", body });',
    )
    font = diff(
        "index.html",
        '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">',
    )
    lookalike = diff("src/lib/models/catalog.ts", '  repo: "https://evil-hf.co/x",')
    assert hosts_flagged(telemetry) == ["telemetry.example.com"]
    assert hosts_flagged(font) == ["fonts.googleapis.com"]
    assert hosts_flagged(lookalike) == ["evil-hf.co"]


def test_any_change_to_the_policy_goes_to_a_human() -> None:
    widened = diff("vite.config.ts", '  "connect-src *",')
    assert hosts_flagged(widened) == ["policy"]


def test_what_doesnt_ship_or_run_is_not_checked() -> None:
    assert (
        hosts_flagged(
            diff("src/lib/models/catalog.ts", '  url: "https://huggingface.co/x",')
        )
        == []
    )
    assert (
        hosts_flagged(
            diff("src/lib/x.ts", "  // see https://developer.mozilla.org/docs")
        )
        == []
    )
    assert (
        hosts_flagged(
            diff("src/lib/privacy/network.test.ts", '  rec("https://evil.example")')
        )
        == []
    )
    assert (
        hosts_flagged(
            diff("e2e/repro/a.spec.ts", '  await page.goto("https://x.example")')
        )
        == []
    )
    assert (
        hosts_flagged(diff("docs/for-it-and-compliance.md", "https://example.org"))
        == []
    )


def test_the_checker_explains_and_the_playground_declares_one() -> None:
    explain = checker(SENTINEL, CSP, TESTS)
    (msg,) = explain(diff("src/a.ts", '  fetch("https://t.example/x")'))
    assert "t.example is not allowed by vite.config.ts" in msg
    found = load_sentinel(Path("targets"), "philippmossier/local-ai-chat-playground")
    assert found is not None and found.policy_file == "vite.config.ts"
