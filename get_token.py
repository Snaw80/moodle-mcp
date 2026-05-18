#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "pywebview>=5.0",
# ]
# ///
"""Fetch a Moodle Web Services token via one of several methods.

Methods:
  local          POST login/token.php with username/password.
                 Works for native Moodle accounts. Fails on SSO-only sites.
  web            Open user/managetoken.php in the browser. After SSO login,
                 copy the displayed token. Best for SSO sites that allow
                 users to self-serve tokens. Fragile: some Moodle themes
                 show a non-API field that looks like a valid token.
  mobile         Default. Opens an embedded webview, completes SSO inside
                 it, and auto-captures the moodlemobile:// redirect token
                 from the final page DOM. Verifies the signature against
                 md5(wwwroot + passport). Works on any SSO site.
  manual-mobile  Same flow as 'mobile' but in your default browser; you
                 paste the resulting moodlemobile:// URL by hand. Fallback
                 if pywebview can't run (e.g. no display, missing libs).

By default the token is written to ./.env (chmod 600) and only a masked
preview is shown. Pass --stdout to print the full token instead (for use
in pipes/redirects); never let it land in scrollback.

Usage:
  ./get_token.py https://moodle.epita.fr
  ./get_token.py https://moodle.epita.fr --method web
  ./get_token.py https://moodle.epita.fr --method local --user jdoe
  ./get_token.py https://moodle.epita.fr --method manual-mobile
  ./get_token.py https://moodle.epita.fr --env-file path/.env
  ./get_token.py https://moodle.epita.fr --stdout > my-token.txt
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


def method_mobile(base: str) -> str | None:
    """Open an embedded webview, complete SSO, auto-capture token."""
    try:
        import webview  # type: ignore
    except ImportError as e:
        print(
            f"  pywebview not available ({e}). Use --method manual-mobile,",
            "  or install pywebview (`uv pip install pywebview` /",
            "  `pip install pywebview`). On Linux you may also need",
            "  libgtk-3 and gir1.2-webkit2-4.1.",
            sep="\n",
            file=sys.stderr,
        )
        return None

    passport = secrets.token_hex(16)
    launch = (
        f"{base}/admin/tool/mobile/launch.php"
        f"?service=moodle_mobile_app&passport={passport}&urlscheme=moodlemobile"
    )

    state: dict[str, str | None] = {"raw": None}

    def on_loaded(window):
        if state["raw"]:
            return
        try:
            html = window.evaluate_js(
                "document.documentElement && document.documentElement.outerHTML"
            )
        except Exception:
            return
        if not html:
            return
        m = _MOODLEMOBILE_RE.search(html)
        if m:
            state["raw"] = m.group(1)
            try:
                window.destroy()
            except Exception:
                pass

    print(f"  Opening login window (passport: {passport[:8]}…).")
    print("  Complete SSO. The window will close automatically on success.")

    window = webview.create_window(
        "Moodle login",
        launch,
        width=900,
        height=720,
    )
    window.events.loaded += on_loaded
    try:
        webview.start()
    except Exception as e:
        print(f"  Webview failed: {e}", file=sys.stderr)
        print("  Try --method manual-mobile instead.", file=sys.stderr)
        return None

    if not state["raw"]:
        print(
            "  No token captured (window closed before SSO completed?).",
            file=sys.stderr,
        )
        return None

    signature, token = _decode_payload(state["raw"])
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
    p.add_argument("moodle_url", help="e.g. https://moodle.epita.fr")
    p.add_argument(
        "--method",
        choices=["auto", "local", "web", "mobile", "manual-mobile"],
        default="auto",
        help=(
            "auto: 'local' (if --user given) → 'mobile' (webview capture). "
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
