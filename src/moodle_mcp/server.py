"""Moodle MCP server.

Exposes Moodle Web Services (REST) as MCP tools. Works with any MCP client
(Claude Code, Claude Desktop, Codex, Cursor, etc.) over stdio.

Required env vars:
    MOODLE_URL    e.g. https://moodle.example.org
    MOODLE_TOKEN  Web Services token (see `mcp-moodle-token`)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("moodle")


def _config() -> tuple[str, str]:
    url = os.environ.get("MOODLE_URL", "").rstrip("/")
    token = os.environ.get("MOODLE_TOKEN", "")
    if not url:
        raise SystemExit("MOODLE_URL env var is required")
    if not token:
        raise SystemExit("MOODLE_TOKEN env var is required")
    return url, token


def _flatten(params: dict[str, Any]) -> dict[str, Any]:
    """Moodle expects PHP-style array params: key[0]=a&key[1]=b."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, list):
            for i, item in enumerate(v):
                out[f"{k}[{i}]"] = item
        else:
            out[k] = v
    return out


async def _call(fn: str, **params: Any) -> Any:
    url, token = _config()
    payload = {
        "wstoken": token,
        "wsfunction": fn,
        "moodlewsrestformat": "json",
        **_flatten(params),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{url}/webservice/rest/server.php", data=payload)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("exception"):
            raise RuntimeError(
                f"Moodle error: {data.get('message')} ({data.get('errorcode')})"
            )
        return data


@mcp.tool()
async def site_info() -> dict:
    """Return Moodle site info and the authenticated user's id/username.

    Use this first to verify the token works.
    """
    return await _call("core_webservice_get_site_info")


@mcp.tool()
async def list_my_courses() -> list[dict]:
    """List courses the authenticated user is enrolled in.

    Returns id, shortname, fullname, category id, and visibility.
    """
    me = await _call("core_webservice_get_site_info")
    courses = await _call("core_enrol_get_users_courses", userid=me["userid"])
    return [
        {
            "id": c["id"],
            "shortname": c.get("shortname"),
            "fullname": c.get("fullname"),
            "category": c.get("category"),
            "visible": c.get("visible", 1),
        }
        for c in courses
    ]


@mcp.tool()
async def get_course_contents(course_id: int) -> list[dict]:
    """Return sections, modules and file URLs for a course.

    File URLs returned in `contents[].fileurl` require the Moodle token
    appended as `?token=...` (or use download_file).

    Args:
        course_id: numeric course id (see list_my_courses)
    """
    return await _call("core_course_get_contents", courseid=course_id)


@mcp.tool()
async def search_courses(query: str, page: int = 0, perpage: int = 20) -> dict:
    """Search the public course catalog by keyword.

    Args:
        query: search text
        page: 0-indexed page
        perpage: results per page (max 100)
    """
    return await _call(
        "core_course_search_courses",
        criterianame="search",
        criteriavalue=query,
        page=page,
        perpage=perpage,
    )


@mcp.tool()
async def list_assignments(course_ids: list[int] | None = None) -> dict:
    """List assignments across given courses (or all enrolled if omitted).

    Args:
        course_ids: optional list of course ids; omit to use all enrolled
    """
    if course_ids is None:
        me = await _call("core_webservice_get_site_info")
        courses = await _call("core_enrol_get_users_courses", userid=me["userid"])
        course_ids = [c["id"] for c in courses]
    return await _call("mod_assign_get_assignments", courseids=course_ids)


@mcp.tool()
async def upcoming_events(limit: int = 20) -> dict:
    """Return upcoming calendar events (deadlines, sessions) for the user.

    Args:
        limit: max number of events to return
    """
    return await _call(
        "core_calendar_get_action_events_by_timesort", limitnum=limit
    )


@mcp.tool()
async def get_user_grades(course_id: int) -> dict:
    """Return the authenticated user's grades for a course.

    Args:
        course_id: numeric course id
    """
    me = await _call("core_webservice_get_site_info")
    return await _call(
        "gradereport_user_get_grade_items",
        courseid=course_id,
        userid=me["userid"],
    )


@mcp.tool()
async def download_file(file_url: str, save_path: str) -> dict:
    """Download a Moodle file (from get_course_contents) to a local path.

    The Moodle token is appended automatically. The target directory is
    created if missing.

    Args:
        file_url: pluginfile.php URL from a module's `contents[].fileurl`
        save_path: absolute local path to write the file to
    """
    _, token = _config()
    sep = "&" if "?" in file_url else "?"
    url = f"{file_url}{sep}token={token}"
    dest = Path(save_path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return {"saved": str(dest), "bytes": len(r.content)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
