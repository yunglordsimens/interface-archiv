"""Process queued observations from the private inbox (Supabase).

Each run:
1. Marks groups as published once their entry is on main.
2. Takes queued groups (oldest first), downloads their files from private storage,
   runs the archive pipeline (draft PR) or the private-note pipeline (saved back to Supabase).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, GEMINI_API_KEY, GEMINI_MODEL (optional), GH_TOKEN, GITHUB_REPOSITORY.
"""
import json
import os
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import ingest  # noqa: E402

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
MAX_PER_RUN = 5


def headers(extra=None):
    h = {"apikey": KEY, "Content-Type": "application/json"}
    if KEY.startswith("eyJ"):
        h["Authorization"] = f"Bearer {KEY}"
    h.update(extra or {})
    return h


def rest(method, path, body=None, extra=None):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers(extra))
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read()
        return json.loads(txt) if txt else None


def patch_group(gid, fields):
    rest("PATCH", f"groups?id=eq.{gid}", fields, {"Prefer": "return=minimal"})


def download(path):
    req = urllib.request.Request(f"{URL}/storage/v1/object/inbox/{urllib.parse.quote(path)}", headers=headers())
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def sync_published():
    waiting = rest("GET", "groups?status=eq.in_review&select=id,entry_id") or []
    if not waiting:
        return
    d = json.load(open(ingest.DATA, encoding="utf-8"))
    on_main = {o.get("submitted_via") for o in d["observations"]}
    for g in waiting:
        if f"inbox:{g['id']}" in on_main:
            patch_group(g["id"], {"status": "published"})
            print(f"published: {g['id']}")


def close_cancelled():
    """Groups cancelled in the inbox (back to New, discarded, merged): close their draft PRs."""
    for g in rest("GET", "groups?status=eq.cancelled&select=id,pr_url") or []:
        if g.get("pr_url") and "/pull/" in g["pr_url"]:
            subprocess.run(["gh", "pr", "close", g["pr_url"], "--delete-branch", "--comment",
                            "Cancelled in the inbox (moved back, discarded or merged into another observation)."],
                           capture_output=True, text=True)
        patch_group(g["id"], {"status": "declined"})
        print(f"closed: {g['id']}")


def load_files(gid):
    ups = rest("GET", f"uploads?group_id=eq.{gid}&select=*&order=sort.asc") or []
    files, audio = [], None
    for u in ups:
        raw = download(u["path"])
        if u["kind"] == "audio":
            audio = audio or {"raw": raw, "mime": (u.get("mime") or "audio/mp4").split(";")[0]}
            continue
        trim = (u.get("trim_start"), u.get("trim_end")) if u["kind"] == "video" else None
        files.append({"raw": raw, "trim": trim})
    return files, audio


def process_archive(g, files, audio):
    branch = f"inbox/{g['id'][:8]}"
    git("checkout", "-B", branch, "origin/main")
    d = json.load(open(ingest.DATA, encoding="utf-8"))
    meta = {"context": g.get("context") or "", "place": g.get("place") or "", "date": g.get("date") or "",
            "category": g.get("category") or "", "force": bool(g.get("force"))}
    if g["source"] == "guest":
        meta["rights"] = "Contributed by a guest who confirmed they took the material. Personal data blurred automatically; reviewed before publication."
        if g.get("guest_name"):
            meta["contributor"] = g["guest_name"]
    via = f"inbox:{g['id']}"
    body, entries, notes = [], [], []

    if g.get("split"):
        groups, unrelated = ingest.plan_split(files, meta["context"])
        if unrelated:
            notes.append(f"Left out as unrelated: files {', '.join(str(i + 1) for i in unrelated)}.")
        for k, grp in enumerate(groups):
            sub = [files[i] for i in grp["files"]]
            m = dict(meta, context=(meta["context"] + "\n" if meta["context"] else "") + f"This part of the batch: {grp['label']}")
            try:
                entry, lines, summary = ingest.build_archive_entry(sub, m, d, via, audio)
            except SystemExit as e:
                notes.append(f"Part {k + 1} ({grp['label']}, files {', '.join(str(i + 1) for i in grp['files'])}) skipped: {e}")
                continue
            entries.append(entry)
            body += [f"# Part {k + 1}: files {', '.join(str(i + 1) for i in grp['files'])}", ""] + lines + [""]
            if summary:
                notes.append(f"{entry['id']}: {summary}")
        if not entries:
            raise SystemExit("Nothing could be built from this batch. " + " ".join(notes))
    else:
        entry, lines, summary = ingest.build_archive_entry(files, meta, d, via, audio)
        entries.append(entry)
        body += lines
        if summary:
            notes.append(summary)

    removed = []
    if g.get("replaces"):
        removed = ingest.remove_entry(d, g["replaces"])
        body = [f"Replaces entry {g['replaces']}, which is removed together with its images.", ""] + body
    with open(ingest.DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    for path in removed:
        git("rm", "-q", "--ignore-unmatch", path, check=False)
    git("add", ingest.DATA, "media/")
    ids = [e["id"] for e in entries]
    label = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
    git("commit", "-m", f"Add observation{'s' if len(ids) > 1 else ''} {label} from inbox")
    git("push", "-f", "origin", branch)
    if notes:
        body = ["### Notes", *[f"- {n}" for n in notes], ""] + body
    title = (f"Observation {ids[0]}: {entries[0]['title']}" if len(ids) == 1
             else f"Observations {label}: {len(ids)} parts of one batch")
    pr = subprocess.run(["gh", "pr", "create", "--head", branch, "--base", "main", "--title", title, "--body", "\n".join(body)],
                        capture_output=True, text=True)
    if pr.returncode == 0:
        url = pr.stdout.strip().splitlines()[-1]
    else:
        existing = subprocess.run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], capture_output=True, text=True)
        url = existing.stdout.strip() or f"https://github.com/{REPO}/compare/main...{branch}?expand=1"
    git("checkout", "main")
    patch_group(g["id"], {"status": "in_review", "entry_id": ",".join(ids),
                          "title": entries[0]["title"] if len(ids) == 1 else f"{len(ids)} observations: " + "; ".join(e["title"] for e in entries)[:300],
                          "pr_url": url, "error": " ".join(notes)[:500] or None})
    print(f"draft {label}: {url}")


def process_private(g, files, audio):
    meta = {"context": g.get("context") or "", "place": g.get("place") or "", "date": g.get("date") or ""}
    res = ingest.build_private_note(files, meta, audio)
    title = (res.get("note") or {}).get("title")
    patch_group(g["id"], {"status": "done", "result": res, "title": title, "error": None})
    print(f"private note: {title}")


def main():
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("fetch", "origin", "main")
    sync_published()
    close_cancelled()
    queued = rest("GET", f"groups?status=eq.queued&select=*&order=created_at.asc&limit={MAX_PER_RUN}") or []
    print(f"queued: {len(queued)}")
    for g in queued:
        patch_group(g["id"], {"status": "processing", "error": None})
        try:
            files, audio = load_files(g["id"])
            if not files:
                raise SystemExit("No photos or videos in this observation.")
            if g["level"] == "private":
                process_private(g, files, audio)
            else:
                process_archive(g, files, audio)
        except BaseException as e:  # SystemExit from the pipeline carries a readable message
            msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            traceback.print_exc()
            patch_group(g["id"], {"status": "failed", "error": msg[:500]})
            git("checkout", "-f", "main", check=False)
            git("clean", "-fd", "media/", check=False)


if __name__ == "__main__":
    main()
