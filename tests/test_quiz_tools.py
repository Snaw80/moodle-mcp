import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moodle_mcp import server


class QuizToolTests(unittest.IsolatedAsyncioTestCase):
    def test_flatten_supports_nested_preflight_data(self):
        result = server._flatten(
            {
                "quizid": 7,
                "preflightdata": [{"name": "quizpassword", "value": "secret"}],
            }
        )

        self.assertEqual(
            result,
            {
                "quizid": 7,
                "preflightdata[0][name]": "quizpassword",
                "preflightdata[0][value]": "secret",
            },
        )

    def test_flatten_encodes_booleans_as_moodle_ints(self):
        result = server._flatten({"forcenew": False, "preview": True})

        self.assertEqual(result, {"forcenew": 0, "preview": 1})
        self.assertIs(type(result["forcenew"]), int)
        self.assertIs(type(result["preview"]), int)

    def test_extract_question_content_includes_formula_images_and_assets(self):
        html = """
        <p>Choose a formula</p>
        <img class="texrender" alt="P \\Rightarrow Q" title="P \\Rightarrow Q"
             src="https://moodle.example/filter/tex/pix.php/formula.svg" />
        <img alt="Proof tree" src="https://moodle.example/pluginfile.php/1/question/questiontext/2/3/tree.png?time=4" />
        <script>ignored()</script>
        """

        content = server._extract_question_content(html)

        self.assertIn("[P \\Rightarrow Q]", content["text"])
        self.assertIn("[Proof tree]", content["text"])
        self.assertEqual(
            content["images"],
            [
                {
                    "alt": "P \\Rightarrow Q",
                    "title": "P \\Rightarrow Q",
                    "url": "https://moodle.example/filter/tex/pix.php/formula.svg",
                    "download_url": "https://moodle.example/filter/tex/pix.php/formula.svg",
                },
                {
                    "alt": "Proof tree",
                    "title": "",
                    "url": "https://moodle.example/pluginfile.php/1/question/questiontext/2/3/tree.png?time=4",
                    "download_url": "https://moodle.example/webservice/pluginfile.php/1/question/questiontext/2/3/tree.png?time=4",
                },
            ],
        )

    def test_moodle_file_download_url_normalizes_pluginfile_and_appends_token(self):
        result = server._moodle_file_download_url(
            "https://moodle.example/pluginfile.php/1/question/questiontext/tree.png?time=4",
            "secret-token",
        )

        self.assertEqual(
            result,
            "https://moodle.example/webservice/pluginfile.php/1/question/questiontext/tree.png?time=4&token=secret-token",
        )

    async def test_get_quiz_qcm_content_reuses_latest_unfinished_attempt(self):
        calls = []

        async def fake_call(fn, **params):
            calls.append((fn, params))
            if fn == "mod_quiz_get_user_attempts":
                return {
                    "attempts": [
                        {"id": 10, "attempt": 1, "state": "inprogress"},
                        {"id": 12, "attempt": 2, "state": "inprogress"},
                    ],
                    "warnings": [{"message": "attempt warning"}],
                }
            if fn == "mod_quiz_get_attempt_summary":
                return {
                    "questions": [
                        {
                            "slot": 1,
                            "type": "multichoice",
                            "html": (
                                "<p>Q?</p>"
                                '<img alt="P \\Rightarrow Q" '
                                'src="https://moodle.example/pluginfile.php/1/tree.png" />'
                            ),
                        }
                    ],
                    "warnings": [{"message": "summary warning"}],
                }
            raise AssertionError(f"Unexpected Moodle call: {fn}")

        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.get_quiz_qcm_content(quiz_id=7)

        self.assertFalse(result["started_new_attempt"])
        self.assertFalse(result["requires_attempt_creation"])
        self.assertEqual(result["attempt"]["id"], 12)
        self.assertEqual(result["questions"][0]["type"], "multichoice")
        self.assertIn("[P \\Rightarrow Q]", result["questions"][0]["text"])
        self.assertEqual(
            result["questions"][0]["images"][0]["download_url"],
            "https://moodle.example/webservice/pluginfile.php/1/tree.png",
        )
        self.assertEqual(
            [fn for fn, _ in calls],
            ["mod_quiz_get_user_attempts", "mod_quiz_get_attempt_summary"],
        )
        self.assertEqual(
            result["warnings"],
            [{"message": "attempt warning"}, {"message": "summary warning"}],
        )

    async def test_get_quiz_qcm_content_requires_permission_before_creating_attempt(self):
        calls = []

        async def fake_call(fn, **params):
            calls.append((fn, params))
            if fn == "mod_quiz_get_user_attempts":
                return {"attempts": [], "warnings": []}
            raise AssertionError(f"Unexpected Moodle call: {fn}")

        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.get_quiz_qcm_content(quiz_id=7)

        self.assertTrue(result["requires_attempt_creation"])
        self.assertFalse(result["started_new_attempt"])
        self.assertIsNone(result["attempt"])
        self.assertEqual(result["questions"], [])
        self.assertIn("No unfinished attempt exists", result["message"])
        self.assertEqual([fn for fn, _ in calls], ["mod_quiz_get_user_attempts"])

    async def test_get_quiz_qcm_content_starts_attempt_when_explicitly_allowed(self):
        calls = []

        async def fake_call(fn, **params):
            calls.append((fn, params))
            if fn == "mod_quiz_get_user_attempts":
                return {"attempts": [], "warnings": []}
            if fn == "mod_quiz_start_attempt":
                return {
                    "attempt": {"id": 22, "attempt": 1, "state": "inprogress"},
                    "warnings": [{"message": "start warning"}],
                }
            if fn == "mod_quiz_get_attempt_summary":
                return {
                    "questions": [{"slot": 1, "type": "multichoice", "html": "<p>Q?</p>"}],
                    "warnings": [],
                    "totalunanswered": 1,
                }
            raise AssertionError(f"Unexpected Moodle call: {fn}")

        preflight_data = [{"name": "quizpassword", "value": "secret"}]
        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.get_quiz_qcm_content(
                quiz_id=7,
                start_if_needed=True,
                preflight_data=preflight_data,
            )

        self.assertTrue(result["started_new_attempt"])
        self.assertFalse(result["requires_attempt_creation"])
        self.assertEqual(result["attempt"]["id"], 22)
        self.assertEqual(result["totalunanswered"], 1)
        self.assertEqual(
            calls[1],
            (
                "mod_quiz_start_attempt",
                {"quizid": 7, "preflightdata": preflight_data, "forcenew": 0},
            ),
        )
        self.assertIs(type(calls[1][1]["forcenew"]), int)

    async def test_get_quiz_qcm_content_returns_warnings_when_attempt_cannot_start(self):
        async def fake_call(fn, **params):
            if fn == "mod_quiz_get_user_attempts":
                return {"attempts": [], "warnings": []}
            if fn == "mod_quiz_start_attempt":
                return {
                    "attempt": {},
                    "warnings": [{"message": "Quiz is not open yet"}],
                }
            raise AssertionError(f"Unexpected Moodle call: {fn}")

        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.get_quiz_qcm_content(
                quiz_id=7,
                start_if_needed=True,
            )

        self.assertFalse(result["started_new_attempt"])
        self.assertTrue(result["requires_attempt_creation"])
        self.assertIsNone(result["attempt"])
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["warnings"], [{"message": "Quiz is not open yet"}])
        self.assertIn("Moodle did not create", result["message"])

    async def test_get_quiz_qcm_content_walks_pages_when_summary_fails(self):
        calls = []

        async def fake_call(fn, **params):
            calls.append((fn, params))
            if fn == "mod_quiz_get_user_attempts":
                return {"attempts": [{"id": 12, "attempt": 1}], "warnings": []}
            if fn == "mod_quiz_get_attempt_summary":
                raise RuntimeError("Moodle error: summary unavailable")
            if fn == "mod_quiz_get_attempt_data":
                page = params["page"]
                return {
                    "attempt": {"id": 12, "currentpage": page},
                    "messages": [f"message {page}"],
                    "nextpage": 1 if page == 0 else -1,
                    "questions": [
                        {"slot": page + 1, "type": "multichoice", "html": f"<p>Q{page}</p>"}
                    ],
                    "warnings": [{"message": f"page warning {page}"}],
                }
            raise AssertionError(f"Unexpected Moodle call: {fn}")

        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.get_quiz_qcm_content(quiz_id=7)

        self.assertEqual([q["slot"] for q in result["questions"]], [1, 2])
        self.assertEqual(result["messages"], ["message 0", "message 1"])
        self.assertEqual(
            [call for call in calls if call[0] == "mod_quiz_get_attempt_data"],
            [
                (
                    "mod_quiz_get_attempt_data",
                    {"attemptid": 12, "page": 0, "preflightdata": []},
                ),
                (
                    "mod_quiz_get_attempt_data",
                    {"attemptid": 12, "page": 1, "preflightdata": []},
                ),
            ],
        )

    async def test_list_quizzes_wraps_moodle_quiz_api(self):
        async def fake_call(fn, **params):
            self.assertEqual(fn, "mod_quiz_get_quizzes_by_courses")
            self.assertEqual(params, {"courseids": [3, 4]})
            return {"quizzes": [{"id": 9, "name": "Quiz"}], "warnings": []}

        with patch.object(server, "_call", side_effect=fake_call):
            result = await server.list_quizzes(course_ids=[3, 4])

        self.assertEqual(result["quizzes"][0]["id"], 9)


if __name__ == "__main__":
    unittest.main()
