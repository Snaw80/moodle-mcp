#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""One-time helper to fetch a Moodle Web Services token.

Usage:
    ./get_token.py <moodle_url> <username>
    ./get_token.py https://moodle.epita.fr jdoe

Prompts for the password, then prints the token to stdout. Save it as
MOODLE_TOKEN in your MCP client config.
"""

from __future__ import annotations

import getpass
import sys

import httpx


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: get_token.py <moodle_url> <username>", file=sys.stderr)
        return 1

    base = sys.argv[1].rstrip("/")
    username = sys.argv[2]
    password = getpass.getpass("Moodle password: ")

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

    if "token" not in data:
        print(f"Error: {data}", file=sys.stderr)
        return 2

    print(data["token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
