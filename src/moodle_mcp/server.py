"""Moodle MCP server.

Exposes Moodle Web Services (REST) as MCP tools. Works with any MCP client
(Claude Code, Claude Desktop, Codex, Cursor, etc.) over stdio.

Required env vars:
    MOODLE_URL    e.g. https://moodle.example.org
    MOODLE_TOKEN  Web Services token (see `mcp-moodle-token`)
"""

from __future__ import annotations

import html
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

    def add(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                add(f"{prefix}[{i}]", item)
        elif isinstance(value, dict):
            for key, item in value.items():
                add(f"{prefix}[{key}]", item)
        elif isinstance(value, bool):
            out[prefix] = int(value)
        else:
            out[prefix] = value

    for k, v in params.items():
        add(k, v)
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


def _normalize_moodle_file_url(file_url: str) -> str:
    parts = urlsplit(file_url)
    path = parts.path
    if "/webservice/pluginfile.php/" not in path:
        path = path.replace("/pluginfile.php/", "/webservice/pluginfile.php/", 1)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _moodle_file_download_url(file_url: str, token: str) -> str:
    parts = urlsplit(_normalize_moodle_file_url(file_url))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
    query.append(("token", token))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


class _QuestionContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.images: list[dict] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "label"}:
            self.parts.append("\n")
        if tag == "img":
            self._handle_image(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def _handle_image(self, attributes: dict[str, str]) -> None:
        class_name = attributes.get("class", "")
        src = attributes.get("src", "")
        if "questionflagimage" in class_name or "theme/image.php" in src:
            return

        alt = attributes.get("alt", "").strip()
        title = attributes.get("title", "").strip()
        label = alt or title
        if label:
            self.parts.append(f" [{label}] ")
        if src:
            self.images.append(
                {
                    "alt": alt,
                    "title": title,
                    "url": src,
                    "download_url": _normalize_moodle_file_url(src),
                }
            )


def _extract_question_content(question_html: str) -> dict:
    parser = _QuestionContentParser()
    parser.feed(question_html or "")
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    images: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for image in parser.images:
        key = (image["alt"], image["title"], image["url"])
        if key in seen:
            continue
        seen.add(key)
        images.append(image)

    return {"text": text, "images": images}


def _enrich_questions(questions: list[dict]) -> list[dict]:
    enriched = []
    for question in questions:
        content = _extract_question_content(question.get("html", ""))
        enriched.append({**question, **content})
    return enriched


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
async def list_quizzes(course_ids: list[int] | None = None) -> dict:
    """List quizzes/QCMs in given courses, or all visible quizzes if omitted.

    Args:
        course_ids: optional list of course ids
    """
    return await _call("mod_quiz_get_quizzes_by_courses", courseids=course_ids or [])


def _new_attempt_required_response(quiz_id: int, warnings: list[dict] | None) -> dict:
    return {
        "quizid": quiz_id,
        "requires_attempt_creation": True,
        "started_new_attempt": False,
        "attempt": None,
        "questions": [],
        "warnings": warnings or [],
        "messages": [],
        "message": (
            "No unfinished attempt exists for this quiz. Ask the user for "
            "permission, then call again with start_if_needed=true to create one."
        ),
    }


def _attempt_not_started_response(quiz_id: int, warnings: list[dict] | None) -> dict:
    return {
        "quizid": quiz_id,
        "requires_attempt_creation": True,
        "started_new_attempt": False,
        "attempt": None,
        "questions": [],
        "warnings": warnings or [],
        "messages": [],
        "message": (
            "Moodle did not create a quiz attempt. Check the returned warnings "
            "for access restrictions, timing rules, or required preflight data."
        ),
    }


def _latest_attempt(attempts: list[dict]) -> dict | None:
    if not attempts:
        return None
    return max(
        attempts,
        key=lambda attempt: (
            attempt.get("attempt", 0) or 0,
            attempt.get("timestart", 0) or 0,
            attempt.get("id", 0) or 0,
        ),
    )


async def _get_attempt_questions(
    attempt_id: int,
    preflight_data: list[dict],
) -> dict:
    try:
        summary = await _call(
            "mod_quiz_get_attempt_summary",
            attemptid=attempt_id,
            preflightdata=preflight_data,
        )
        return {
            "questions": _enrich_questions(summary.get("questions", [])),
            "warnings": summary.get("warnings", []),
            "messages": [],
            "totalunanswered": summary.get("totalunanswered"),
            "source": "summary",
        }
    except RuntimeError:
        questions: list[dict] = []
        warnings: list[dict] = []
        messages: list[str] = []
        page = 0
        seen_pages: set[int] = set()

        while page >= 0 and page not in seen_pages:
            seen_pages.add(page)
            data = await _call(
                "mod_quiz_get_attempt_data",
                attemptid=attempt_id,
                page=page,
                preflightdata=preflight_data,
            )
            questions.extend(_enrich_questions(data.get("questions", [])))
            warnings.extend(data.get("warnings", []))
            messages.extend(data.get("messages", []))
            page = data.get("nextpage", -1)

        return {
            "questions": questions,
            "warnings": warnings,
            "messages": messages,
            "totalunanswered": None,
            "source": "pages",
        }


@mcp.tool()
async def get_quiz_qcm_content(
    quiz_id: int,
    start_if_needed: bool = False,
    preflight_data: list[dict] | None = None,
) -> dict:
    """Return rendered QCM/quiz questions from the current attempt.

    The tool reuses the latest unfinished attempt. If no unfinished attempt
    exists, it returns `requires_attempt_creation=true` unless
    `start_if_needed` is explicitly true.

    Args:
        quiz_id: quiz instance id (see list_quizzes)
        start_if_needed: create a new attempt only when no unfinished attempt exists
        preflight_data: optional Moodle preflight data, e.g. quiz password entries
    """
    preflight_data = preflight_data or []
    attempts_response = await _call(
        "mod_quiz_get_user_attempts",
        quizid=quiz_id,
        status="unfinished",
    )
    attempt = _latest_attempt(attempts_response.get("attempts", []))

    if attempt is None:
        if not start_if_needed:
            return _new_attempt_required_response(
                quiz_id,
                attempts_response.get("warnings", []),
            )
        start_response = await _call(
            "mod_quiz_start_attempt",
            quizid=quiz_id,
            preflightdata=preflight_data,
            forcenew=0,
        )
        attempt = start_response.get("attempt")
        started_new_attempt = True
        start_warnings = start_response.get("warnings", [])
        if not attempt or "id" not in attempt:
            return _attempt_not_started_response(
                quiz_id,
                [*attempts_response.get("warnings", []), *start_warnings],
            )
    else:
        started_new_attempt = False
        start_warnings = []

    question_data = await _get_attempt_questions(attempt["id"], preflight_data)
    warnings = [
        *attempts_response.get("warnings", []),
        *start_warnings,
        *question_data["warnings"],
    ]
    result = {
        "quizid": quiz_id,
        "requires_attempt_creation": False,
        "started_new_attempt": started_new_attempt,
        "attempt": attempt,
        "questions": question_data["questions"],
        "warnings": warnings,
        "messages": question_data["messages"],
        "question_source": question_data["source"],
    }
    if question_data["totalunanswered"] is not None:
        result["totalunanswered"] = question_data["totalunanswered"]
    return result


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
    url = _moodle_file_download_url(file_url, token)
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
