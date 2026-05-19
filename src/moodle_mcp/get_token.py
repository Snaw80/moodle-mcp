"""Fetch a Moodle Web Services token via one of several methods.

Methods:
  local          POST login/token.php with username/password.
                 Works for native Moodle accounts. Fails on SSO-only sites.
  web            Open user/managetoken.php in the browser. After SSO login,
                 copy the displayed token. Best for SSO sites that allow
                 users to self-serve tokens. Fragile: some Moodle themes
                 show a non-API field that looks like a valid token.
  mobile         Default. Launches a Playwright-controlled Chromium, you
                 complete SSO inside it, and the moodlemobile:// redirect
                 is captured at the network level. Verifies the signature
                 against md5(wwwroot + passport). Works with Microsoft
                 Azure AD, Google, SAML, OAuth — any real-browser SSO.
                 First run downloads Chromium (~150MB, one-time).
  manual-mobile  Same flow but in your default browser; you paste the
                 resulting moodlemobile:// URL by hand. Fallback when
                 Playwright can't run (e.g. headless server, no GUI).

By default the token is written to ./.env (chmod 600) and only a masked
preview is shown. Pass --stdout to print the full token instead (for use
in pipes/redirects); never let it land in scrollback.

Usage:
  mcp-moodle-token https://moodle.example.org
  mcp-moodle-token https://moodle.example.org --method web
  mcp-moodle-token https://moodle.example.org --method local --user jdoe
  mcp-moodle-token https://moodle.example.org --method manual-mobile
  mcp-moodle-token https://moodle.example.org --env-file path/.env
  mcp-moodle-token https://moodle.example.org --stdout > my-token.txt
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import re
import secrets
import sys
import webbrowser
from pathlib import Path

import httpx

_MOODLEMOBILE_RE = re.compile(r"moodlemobile://token=([A-Za-z0-9+/=]+)")


def _decode_payload(b64: str) -> tuple[str | None, str | None]:
    """Decode a moodlemobile token payload into (signature, token)."""
    try:
        decoded = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Base64 decode failed: {e}", file=sys.stderr)
        return None, None
    parts = decoded.split(":::")
    if len(parts) < 2:
        print(f"  Unexpected payload: {decoded[:80]}", file=sys.stderr)
        return None, None
    return parts[0], parts[1]


def _verify_signature(signature: str, base: str, passport: str) -> bool:
    """Moodle signs the response with md5(wwwroot + passport)."""
    expected = hashlib.md5(f"{base}{passport}".encode()).hexdigest()
    return signature == expected


def method_local(base: str, username: str | None) -> str | None:
    if username is None:
        username = input("  Username: ").strip()
    password = getpass.getpass("  Password: ")
    try:
        r = httpx.post(
            f"{base}/login/token.php",
            data={
                "username": username,
                "password": password,
                "service": "moodle_mobile_app",
            },
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        print(f"  HTTP error: {e}", file=sys.stderr)
        return None

    if "token" in data:
        return data["token"]
    err = data.get("errorcode") or data.get("error") or data
    print(f"  Rejected: {err}", file=sys.stderr)
    return None


def method_web(base: str) -> str | None:
    url = f"{base}/user/managetoken.php"
    print(f"  Opening {url}")
    print("  Complete SSO if prompted, then look under 'Moodle mobile web")
    print("  service' (or similar). Copy the token and paste it below.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    token = getpass.getpass("  Paste token (hidden, empty to skip): ").strip()
    return token or None


def _ensure_playwright_chromium() -> bool:
    """Install Playwright's Chromium binary on demand, once."""
    import subprocess

    print(
        "  Playwright's Chromium isn't installed yet.",
        "  Installing now (~150MB, one-time)…",
        sep="\n",
        file=sys.stderr,
    )
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=False,
    )
    return result.returncode == 0


def method_mobile(base: str) -> str | None:
    """Launch a Playwright-controlled Chromium, auto-capture token.

    Plain pywebview-style approaches stall inside Microsoft Azure AD's
    auto-form-submit step (third-party cookies and SSO JS quirks). A real
    Chromium instance driven by Playwright handles every common SSO
    provider, and Playwright lets us intercept the moodlemobile:// URL
    at the network-request layer regardless of whether Moodle emits a
    server-side 303 or a JS-based redirect.
    """
    try:
        from playwright.sync_api import (  # type: ignore
            Error as PlaywrightError,
            sync_playwright,
        )
    except ImportError as e:
        print(
            f"  playwright not available ({e}).",
            "  Use --method manual-mobile, or run:",
            "    pip install playwright && playwright install chromium",
            sep="\n",
            file=sys.stderr,
        )
        return None

    import time

    passport = secrets.token_hex(16)
    launch = (
        f"{base}/admin/tool/mobile/launch.php"
        f"?service=moodle_mobile_app&passport={passport}&urlscheme=moodlemobile"
    )

    captured: dict[str, str | None] = {"url": None}

    print(f"  Launching Chromium (passport: {passport[:8]}…).")
    print("  Complete SSO in the window that opens. It will close itself")
    print("  the moment the moodlemobile:// redirect fires.")

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False)
            except PlaywrightError as e:
                msg = str(e)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    if not _ensure_playwright_chromium():
                        print("  Chromium install failed.", file=sys.stderr)
                        return None
                    browser = p.chromium.launch(headless=False)
                else:
                    raise

            context = browser.new_context()
            page = context.new_page()

            def on_request(request):
                if captured["url"]:
                    return
                if request.url.startswith("moodlemobile://"):
                    captured["url"] = request.url

            def on_response(response):
                # Some Moodle versions emit a server-side 303 instead of JS.
                # The Location header carries the moodlemobile URL.
                if captured["url"]:
                    return
                if 300 <= response.status < 400:
                    loc = response.headers.get("location", "")
                    if loc.startswith("moodlemobile://"):
                        captured["url"] = loc

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(launch, wait_until="domcontentloaded")
            except PlaywrightError:
                # Goto can raise once Chromium hits moodlemobile:// — ignore.
                pass

            deadline = time.time() + 300
            last_url = None
            while time.time() < deadline and captured["url"] is None:
                if page.is_closed():
                    print("  Window closed before SSO finished.", file=sys.stderr)
                    break
                try:
                    current = page.url
                except PlaywrightError:
                    current = ""
                if current and current != last_url:
                    shown = current.split("?", 1)[0]
                    print(
                        f"  [{time.strftime('%H:%M:%S')}] page: {shown[:90]}",
                        file=sys.stderr,
                    )
                    last_url = current
                # Secondary path: scrape the DOM for a JS-emitted URL.
                if not captured["url"]:
                    try:
                        m = _MOODLEMOBILE_RE.search(page.content())
                        if m:
                            captured["url"] = f"moodlemobile://token={m.group(1)}"
                    except PlaywrightError:
                        pass
                page.wait_for_timeout(400)

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        print(f"  Playwright error: {e}", file=sys.stderr)
        return None

    raw = captured["url"]
    if not raw:
        print(
            "  Timed out without capturing a token.",
            "  Re-run with --method manual-mobile as a fallback.",
            sep="\n",
            file=sys.stderr,
        )
        return None

    if "token=" not in raw:
        print(f"  Unexpected capture: {raw[:80]}", file=sys.stderr)
        return None

    b64 = raw.split("token=", 1)[1].split("&", 1)[0].split("#", 1)[0]
    signature, token = _decode_payload(b64)
    if not token:
        return None
    if signature and not _verify_signature(signature, base, passport):
        print(
            f"  WARNING: signature mismatch (got {signature[:8]}…). "
            "Token captured but origin not verified — refusing.",
            file=sys.stderr,
        )
        return None
    return token


def method_manual_mobile(base: str) -> str | None:
    passport = secrets.token_hex(16)
    launch = (
        f"{base}/admin/tool/mobile/launch.php"
        f"?service=moodle_mobile_app&passport={passport}&urlscheme=moodlemobile"
    )
    print(f"  Opening {launch}")
    print("  Complete SSO. The browser will try to open a moodlemobile://")
    print("  URL and show an error — that's expected. Copy the full URL")
    print("  from the address bar (or the error message) and paste below.")
    try:
        webbrowser.open(launch)
    except Exception:
        pass

    raw = getpass.getpass("  Paste moodlemobile://... URL (hidden): ").strip()
    if not raw or "token=" not in raw:
        print("  No token= found in URL", file=sys.stderr)
        return None

    b64 = raw.split("token=", 1)[1].split("&", 1)[0].split("#", 1)[0]
    signature, token = _decode_payload(b64)
    if not token:
        return None
    if signature and not _verify_signature(signature, base, passport):
        print(
            f"  WARNING: signature mismatch (got {signature[:8]}…). "
            "Token captured but origin not verified — refusing.",
            file=sys.stderr,
        )
        return None
    return token


def write_env(env_file: Path, base: str, token: str) -> None:
    env_file = env_file.expanduser().resolve()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    preserved: list[str] = []
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(("MOODLE_URL=", "MOODLE_TOKEN=")):
                continue
            preserved.append(line)
    body = "\n".join(preserved + [f"MOODLE_URL={base}", f"MOODLE_TOKEN={token}"])
    env_file.write_text(body + "\n")
    env_file.chmod(0o600)
    print(f"  Wrote {env_file} (chmod 600)", file=sys.stderr)


def mask(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}…{token[-4:]} ({len(token)} chars)"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fetch a Moodle Web Services token.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("moodle_url", help="e.g. https://moodle.example.org")
    p.add_argument(
        "--method",
        choices=["auto", "local", "web", "mobile", "manual-mobile"],
        default="auto",
        help=(
            "auto: 'local' (if --user given) → 'mobile' (Playwright Chromium). "
            "'mobile' is the cross-SSO default."
        ),
    )
    p.add_argument("--user", help="Username (enables auto-trying 'local').")
    p.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Where to write MOODLE_URL/MOODLE_TOKEN (default: ./.env, chmod 600).",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Print the raw token to stdout instead of writing a file.",
    )
    args = p.parse_args()

    base = args.moodle_url.rstrip("/")

    if args.method == "auto":
        methods = (["local"] if args.user else []) + ["mobile"]
    else:
        methods = [args.method]

    runners = {
        "local": lambda: method_local(base, args.user),
        "web": lambda: method_web(base),
        "mobile": lambda: method_mobile(base),
        "manual-mobile": lambda: method_manual_mobile(base),
    }

    token: str | None = None
    for i, m in enumerate(methods):
        print(f"\n→ Method: {m}")
        token = runners[m]()
        if token:
            break
        if i < len(methods) - 1:
            ans = input("  Try next method? [Y/n] ").strip().lower()
            if ans == "n":
                break

    if not token:
        print("\nNo token obtained.", file=sys.stderr)
        return 2

    if args.stdout:
        print(token)
    else:
        write_env(args.env_file, base, token)
        print(f"  Token: {mask(token)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
