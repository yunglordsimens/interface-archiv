# Korea Interface Archive

A research archive of digital and physical interfaces of everyday life in South Korea: apps, services, kiosks, terminals, delivery and payment systems, documented on site.

- Site: https://yunglordsimens.github.io/interface-archiv/
- Data: [`data/observations.json`](data/observations.json)
- MCP server for AI clients: [`mcp/server.js`](mcp/server.js)

## What an entry contains

Each observation keeps four levels apart:

- **observed** — what was seen directly
- **confirmed** — what external sources support (with links and access dates)
- **interpretation** — the author's reading
- **open** — questions and hypotheses

It also records **access** (what it takes to use the interface: physical presence, a Korean phone number, a residence card, a Korean card or address, identity verification) and a **breakdown** for reuse as a reference:

- `steps` — the interaction step by step, with original Korean text and a translation
- `elements` — interface elements with their role and text
- `patterns` — recurring solutions, defined once in `patterns` at the top of the file

Personal data in screenshots is blurred before publication.

## Add an observation

1. Open a new issue and choose **New observation** (works from the GitHub mobile app).
2. Drag in photos, screenshots or short videos (up to 10 MB), write a line about what happened, add place and date.
3. A GitHub Action sends the images to Gemini, which drafts the entry (steps, elements with Korean text and translation, patterns, access) and marks personal data. The script blurs it, compresses the images, strips location data and opens a pull request. Videos are cut into still frames at the key moments; a short silent clip is kept only when motion matters and nothing personal is visible. Every image gets a perceptual hash, and near-duplicates of existing material are flagged in the pull request.
4. Check the draft and merge it. The site and the MCP server update on their own.

Issues from the owner and collaborators are processed right away. Issues from anyone else wait until the owner adds the `approved` label. The model is set by the `GEMINI_MODEL` repository variable (default: the latest Gemini Flash); the key is the `GEMINI_API_KEY` secret.

## Inbox (phone)

- **Add** (`/add/`): works as an app from the phone home screen (Share → Add to Home Screen). Pick photos or videos from the camera or gallery, record an optional voice note, send. The owner's uploads go to a private inbox; guests' submissions wait for approval.
- **Inbox** (`/inbox/`, owner only, sign-in by email link): sort new files, discard, group several files into one observation, trim videos, mark it as *Archive* or *Private note*, approve guest submissions, read private notes.
- Originals are stored in a private Supabase bucket. Row-level security lets guests only add, and only the owner read.
- `.github/workflows/process-inbox.yml` runs every 10 minutes: queued archive observations become draft pull requests through the same Gemini pipeline; private notes are saved back to the inbox and never reach this repository.

## Use it from an AI client (MCP)

The archive can be connected to Claude Desktop or any MCP client. Add this to the client's MCP configuration:

```json
{
  "mcpServers": {
    "korea-interface-archive": {
      "command": "npx",
      "args": ["-y", "github:yunglordsimens/interface-archiv"]
    }
  }
}
```

Tools: `search_observations`, `get_observation`, `list_patterns`, `access_requirements`. The server is read-only and loads the public data file from this repository. Set `KIA_DATA` to a local path to use a local copy.

## Scope and limits

This version covers South Korea and is collected mostly by one person living in Daejeon in autumn 2026. It does not describe the country as a whole. The method, schema and code do not depend on the country and can be reused for another one.

## Licenses

- Code (site and MCP server): MIT, see [LICENSE](LICENSE).
- Photos, screenshots and texts: see [LICENSE-CONTENT.md](LICENSE-CONTENT.md). Screenshots may contain third-party brands and material that keep their own rights.

## Support

Field documentation was supported by a travel stipend from Nadání Josefa, Marie a Zdeňky Hlávkových during an exchange semester at Chungnam National University.
