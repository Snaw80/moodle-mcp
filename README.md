# mcp-moodle

An [MCP](https://modelcontextprotocol.io) server that exposes
[Moodle Web Services](https://docs.moodle.org/dev/Web_services) to any
MCP-compatible AI assistant — Claude Code, Claude Desktop, Cursor, Codex,
and others.

Ask your assistant things like _"what's due this week?"_, _"list my courses"_,
_"download the slides from CS101 week 3"_ — without leaving the chat.

## Features

- **`site_info`** — verify the token and get the authenticated user
- **`list_my_courses`** — courses you're enrolled in
- **`get_course_contents`** — sections, modules, file URLs
- **`search_courses`** — search the public catalog
- **`list_assignments`** — assignments across one or all courses
- **`upcoming_events`** — calendar deadlines and sessions
- **`get_user_grades`** — your grades for a course
- **`download_file`** — save any Moodle file locally (token appended automatically)

Works with any Moodle 3.5+ instance that has Web Services enabled.

## Install

The recommended way is [`uv`](https://docs.astral.sh/uv/) — no virtualenv to manage:

```bash
# One-off run (no install)
uvx mcp-moodle

# Or persist as a tool
uv tool install mcp-moodle
```

Plain pip works too:

```bash
pip install mcp-moodle
```

## Get a token

Moodle Web Services require a personal token. The package ships a helper that
handles every common login flow — native accounts, SSO (Microsoft, Google,
SAML, OAuth), or manual paste:

```bash
# Default: opens a Chromium window, you complete SSO, token is captured
uvx --from "mcp-moodle[token]" mcp-moodle-token https://moodle.example.org

# Native (non-SSO) account
uvx --from "mcp-moodle[token]" mcp-moodle-token https://moodle.example.org \
  --method local --user jdoe

# Headless server fallback (paste the moodlemobile:// URL by hand)
uvx --from "mcp-moodle[token]" mcp-moodle-token https://moodle.example.org \
  --method manual-mobile
```

The token is written to `./.env` (chmod 600) as `MOODLE_URL` and `MOODLE_TOKEN`.
Pass `--stdout` to print it to stdout instead.

> The `[token]` extra pulls in Playwright. First run downloads Chromium
> (~150 MB, one-time). Skip the extra if you only ever use `--method local`,
> `--method web`, or `--method manual-mobile`.

## Configure your MCP client

### Claude Code

```bash
claude mcp add moodle \
  --env MOODLE_URL=https://moodle.example.org \
  --env MOODLE_TOKEN=your_token_here \
  -- uvx mcp-moodle
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "uvx",
      "args": ["mcp-moodle"],
      "env": {
        "MOODLE_URL": "https://moodle.example.org",
        "MOODLE_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Cursor / other clients

Any MCP client that supports stdio servers works the same way: command
`uvx`, args `["mcp-moodle"]`, env `MOODLE_URL` and `MOODLE_TOKEN`.

## Verify it works

In your MCP client, ask: _"call the moodle site_info tool"_. You should see
your name, username, and the site URL.

## Development

```bash
git clone git@github.com:Snaw80/moodle-mcp.git
cd moodle-mcp
uv sync --all-extras
uv run mcp-moodle
```

## Security notes

- Your token is the equivalent of a password for Moodle Web Services — keep
  `.env` out of version control (the included `.gitignore` already does this).
- The server reads `MOODLE_TOKEN` from the environment and never logs it.
- `download_file` appends the token to the URL; that URL is not logged either,
  but be mindful if your client echoes tool arguments.

## License

MIT — see [LICENSE](LICENSE).
