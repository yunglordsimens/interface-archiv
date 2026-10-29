"""Turn a "New observation" issue into a draft archive entry.

1. Reads the issue and downloads its photos and videos.
2. Videos: Gemini picks key moments; ffmpeg cuts them into still frames and,
   only when motion matters and nothing personal is visible, a short silent clip.
3. Photos and frames: Gemini drafts the entry and marks personal data; the
   script blurs it, compresses images and drops EXIF/GPS.
4. Duplicates: every image gets a perceptual hash; near matches against the
   archive (and inside the submission) are flagged for the reviewer.
5. Appends the entry to data/observations.json. The workflow then opens a PR.

Env: GEMINI_API_KEY, GEMINI_MODEL (optional), GH_TOKEN, ISSUE_NUMBER, REPO.
Local test: MOCK_RESPONSE / MOCK_VIDEO = paths to JSON files skip Gemini calls.
"""
import base64
import datetime
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from PIL import Image, ImageFilter, ImageOps

DATA = "data/observations.json"
OUT = ".ingest"
MODELS = list(dict.fromkeys(m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"] if m))
DUP_DISTANCE = 10          # max differing bits (of 64) to call two images near-duplicates
MAX_FRAMES_PER_VIDEO = 6
MAX_CLIP_SECONDS = 20
VIDEO_MIME = {b"ftypqt": "video/quicktime"}


# ---------- input ----------

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def gh(path):
    return json.loads(http(f"https://api.github.com/repos/{os.environ['REPO']}/{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json"}))


def sections(body):
    out, key = {}, None
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.*)", line)
        if m:
            key = m.group(1).strip()
            out[key] = []
        elif key:
            out[key].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def clean(v):
    return "" if v in ("", "_No response_", "Not sure") else v


def fetch(url):
    headers = {"User-Agent": "interface-archive-ingest"}
    if "github.com" in url or "githubusercontent.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    try:
        return http(url, headers=headers)
    except urllib.error.HTTPError:
        return http(url, headers={"User-Agent": "interface-archive-ingest"})


def attachment_urls(text):
    """Images come as ![](url) or <img src>; videos as a bare user-attachments URL on its own line."""
    found = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text)
    found += re.findall(r'<img[^>]+src="(https?://[^"]+)"', text)
    found += re.findall(r'<video[^>]+src="(https?://[^"]+)"', text)
    found += re.findall(r"^\s*(https://github\.com/user-attachments/assets/[\w-]+)\s*$", text, re.M)
    found += re.findall(r"^\s*(https?://\S+\.(?:mp4|mov|webm))\s*$", text, re.M | re.I)
    return list(dict.fromkeys(found))


def classify(raw):
    """Return ('image', PIL.Image) or ('video', mime)."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return "image", ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        pass
    head = raw[:32]
    if b"ftyp" in head:
        return "video", "video/quicktime" if b"ftypqt" in head else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"
    raise SystemExit("An attachment is neither an image nor a supported video (mp4, mov, webm).")


# ---------- video ----------

def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def frame_at(path, t):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out = f.name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", path, "-frames:v", "1", out], check=True)
    img = Image.open(out).convert("RGB")
    os.unlink(out)
    return img


def make_clip(path, start, end, dest):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", path,
                    "-an", "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "28", "-movflags", "+faststart", "-pix_fmt", "yuv420p", dest], check=True)


VIDEO_PROMPT = """You help document everyday interfaces in South Korea. For each video, in order, return ONLY JSON:
{"videos": [{"index": 0,
  "moments": [{"t": seconds, "why": "what this moment shows"}],
  "clip": {"keep": true or false, "start": seconds, "end": seconds,
           "reason": "why motion matters here, or why not",
           "personal_data_visible": true or false}}]}
Rules:
- moments: up to %d per video, the moments that best show each step or state of the interface. Seconds as numbers.
- clip.keep is true only if motion itself matters (an animation, a transition, a timing, a moving device) and cannot be
  shown by still frames. Keep it short, at most %d seconds.
- personal_data_visible is true if any private phone number, name, address, face, licence plate, card, chat message
  or tracking number is visible at any time in the clip range. If unsure, true.
""" % (MAX_FRAMES_PER_VIDEO, MAX_CLIP_SECONDS)


# ---------- Gemini ----------

def gemini(parts, mock_env):
    if os.environ.get(mock_env):
        return json.load(open(os.environ[mock_env]))
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        res = None
        for wait in (0, 10, 30, 60):
            time.sleep(wait)
            try:
                res = json.loads(http(url, body, {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]}))
                break
            except urllib.error.HTTPError as e:
                last = f"{model}: HTTP {e.code} {e.read()[:200]!r}"
                print(last, file=sys.stderr)
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                last = f"{model}: {type(e).__name__} {e}"
                print(last, file=sys.stderr)
        if res is None:
            if last and " HTTP 4" in last and " HTTP 429" not in last and " HTTP 404" not in last:
                raise SystemExit(last)
            continue
        txt = res["candidates"][0]["content"]["parts"][0]["text"]
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
        usage = res.get("usageMetadata", {})
        print(f"model={model} tokens in={usage.get('promptTokenCount')} out={usage.get('candidatesTokenCount')}")
        return json.loads(txt)
    raise SystemExit(f"No Gemini model available. Last error: {last}")


def image_part(img):
    im = img.copy()
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


PROMPT = """You document everyday interfaces in South Korea for a research archive.
Return ONLY a JSON object, no prose, with this shape:

{
  "entry": {
    "title": "short English name, e.g. 'Ramen shop ordering kiosk'",
    "category": "Digital | Physical | Services | Systems",
    "service": "brand or operator if visible, else ''",
    "scenario": "one sentence: what a person does here",
    "access": {"requires": ["only requirements that are visible or stated by the author"],
               "unverified": ["plausible requirements that need checking, phrased as questions"]},
    "observed": ["facts visible in the images or stated by the author, one per item"],
    "interpretation": ["cautious readings, clearly not facts"],
    "open": ["questions and hypotheses worth checking"],
    "breakdown": {
      "steps": [{"n": 1, "where": "screen or object", "action": "what the user does",
                 "text_ko": "Korean text exactly as shown", "text_en": "translation"}],
      "elements": [{"name": "element", "role": "what it does",
                    "text_ko": "Korean text exactly as shown", "text_en": "translation"}],
      "patterns": ["pattern ids"]
    }
  },
  "new_patterns": {"new-id": {"label": "short label", "description": "one sentence"}},
  "media": [{"index": 0, "caption": "what this image shows",
             "redact": [{"box_2d": [ymin, xmin, ymax, xmax], "what": "phone number"}]}]
}

Rules:
- Write in English. Keep Korean text verbatim in text_ko; never invent text you cannot read.
- Only state what the images show or the author says. No statistics, history or claims about Korea in general.
  Anything you know from outside goes into "open" as a question, never into "observed".
- Steps only if the images show a sequence; otherwise an empty list. Frames taken from a video are in time order.
- Patterns: reuse an existing id when it fits (list below). Add a new id to new_patterns only for a clearly recurring solution. Ids are lowercase-with-hyphens.
- Never copy personal data into any text field: phone numbers of private people, personal names, home or delivery addresses,
  tracking or order numbers, card or account numbers. Write "(blurred)" instead.
- media.redact: boxes around every piece of personal data visible in each image: private phone numbers, personal names,
  addresses, tracking/order/booking numbers, card numbers, faces of people, licence plates, QR codes or barcodes tied to a person,
  parcel labels, chat messages from private people. Do NOT blur public business information (store names, prices,
  customer service numbers, product names). box_2d uses coordinates normalised to 0-1000. Pad boxes slightly. If unsure, include it.
- One media item per image, same order as given, index starting at 0.
"""


# ---------- images ----------

def dhash(img):
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def distance(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


PHONE = re.compile(r"(?<!\d)(01[016789])[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")


def scrub(v):
    if isinstance(v, str):
        return PHONE.sub(r"\1-(blurred)", v)
    if isinstance(v, list):
        return [scrub(x) for x in v]
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items()}
    return v


def redact(img, boxes):
    w, h = img.size
    for b in boxes:
        try:
            y0, x0, y1, x1 = [float(c) for c in b["box_2d"]]
        except (KeyError, TypeError, ValueError):
            continue
        pad = 0.01
        box = (max(0, int((x0 / 1000 - pad) * w)), max(0, int((y0 / 1000 - pad) * h)),
               min(w, int((x1 / 1000 + pad) * w)), min(h, int((y1 / 1000 + pad) * h)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        reg = img.crop(box)
        r = max(8, min(box[2] - box[0], box[3] - box[1]) // 6)
        for _ in range(3):
            reg = reg.filter(ImageFilter.GaussianBlur(r))
        img.paste(reg, box)
    return img


def save(img, path):
    width = 860 if img.height / img.width > 1.8 else 1200
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.save(path, "JPEG", quality=80, optimize=True)  # re-encoding drops EXIF and GPS


# ---------- main ----------

def main():
    issue = gh(f"issues/{os.environ['ISSUE_NUMBER']}")
    s = sections(issue["body"])
    attach = s.get("Photos, screenshots or videos") or s.get("Photos or screenshots") or issue["body"] or ""
    urls = attachment_urls(attach)
    if not urls:
        raise SystemExit("No photos or videos found in the issue. Drag them into the first field.")

    items = []  # {"kind": "image"|"frame", "img": PIL, "source": url, "t": seconds}
    videos = []  # {"path", "mime", "duration", "source"}
    for u in urls:
        raw = fetch(u)
        kind, val = classify(raw)
        if kind == "image":
            items.append({"kind": "image", "img": val, "source": u, "sha": hashlib.sha256(raw).hexdigest()})
        else:
            ext = {"video/quicktime": ".mov", "video/webm": ".webm"}.get(val, ".mp4")
            f = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            f.write(raw)
            f.close()
            videos.append({"path": f.name, "mime": val, "duration": ffprobe_duration(f.name), "source": u, "bytes": len(raw)})

    clips_plan, video_notes = [], []
    if videos:
        too_big = [v for v in videos if v["bytes"] > 19_000_000]
        if too_big:
            raise SystemExit("A video is larger than 19 MB. Trim it before uploading.")
        parts = [{"text": VIDEO_PROMPT}] + [{"inline_data": {"mime_type": v["mime"], "data": base64.b64encode(open(v["path"], "rb").read()).decode()}} for v in videos]
        plan = gemini(parts, "MOCK_VIDEO").get("videos", [])
        by_vi = {p.get("index", i): p for i, p in enumerate(plan)}
        for vi, v in enumerate(videos):
            p = by_vi.get(vi, {})
            moments = sorted({round(float(m["t"]), 2) for m in p.get("moments", []) if "t" in m})[:MAX_FRAMES_PER_VIDEO]
            if not moments:
                moments = [round(v["duration"] * k / 4, 2) for k in (1, 2, 3)]
            for t in moments:
                t = min(t, max(0.0, v["duration"] - 0.1))
                img = frame_at(v["path"], t)
                items.append({"kind": "frame", "img": img, "source": v["source"], "t": t})
            c = p.get("clip") or {}
            note = f"video {vi + 1}: {len(moments)} frames"
            if c.get("keep") and not c.get("personal_data_visible", True):
                start = max(0.0, float(c.get("start", 0)))
                end = min(float(c.get("end", start + MAX_CLIP_SECONDS)), start + MAX_CLIP_SECONDS, v["duration"])
                if end - start >= 1:
                    clips_plan.append({"video": v, "start": start, "end": end, "reason": c.get("reason", "")})
                    note += f", clip {start:.1f}–{end:.1f}s kept ({c.get('reason', '')})"
            elif c.get("keep"):
                note += ", clip not kept: personal data may be visible"
            else:
                note += ", no clip: stills are enough"
            video_notes.append(note)

    d = json.load(open(DATA, encoding="utf-8"))
    patterns = d.setdefault("patterns", {})

    # duplicates: against the archive and inside this submission
    for it in items:
        it["hash"] = dhash(it["img"])
    known = [(m["src"], m["hash"]) for o in d["observations"] for m in o.get("media", []) if m.get("hash")]
    dup_notes = []
    for i, it in enumerate(items):
        for src, h in known:
            dist = distance(it["hash"], h)
            if dist <= DUP_DISTANCE:
                dup_notes.append(f"image {i + 1} looks like `{src}` (difference {dist}/64)")
        for j in range(i):
            if it["kind"] == "image" and items[j]["kind"] == "image" and distance(it["hash"], items[j]["hash"]) <= 3:
                dup_notes.append(f"image {i + 1} looks like image {j + 1} in this submission")

    context = clean(s.get("What happened", ""))
    place = clean(s.get("Place", ""))
    date = clean(s.get("Date", ""))
    category = clean(s.get("Category", ""))
    order = "; ".join(f"image {i}: " + ("photo" if it["kind"] == "image" else f"frame at {it['t']:.1f}s of a video")
                      for i, it in enumerate(items))
    ctx = (f"What happened: {context}\nPlace: {place or 'not given'}\nDate: {date or 'not given'}\n"
           f"Category chosen: {category or 'not given'}\nImages: {order}")
    prompt = PROMPT + "\nExisting patterns:\n" + json.dumps({k: v["label"] for k, v in patterns.items()}, ensure_ascii=False) + "\n\nAuthor's context:\n" + ctx
    res = scrub(gemini([{"text": prompt}] + [image_part(it["img"]) for it in items], "MOCK_RESPONSE"))

    ids = [int(o["id"]) for o in d["observations"] if str(o["id"]).isdigit()]
    oid = f"{(max(ids) + 1 if ids else 1):03d}"
    os.makedirs("media", exist_ok=True)
    media, redactions = [], []
    by_index = {m.get("index", i): m for i, m in enumerate(res.get("media", []))}
    for i, it in enumerate(items):
        m = by_index.get(i, {})
        boxes = m.get("redact", []) or []
        redact(it["img"], boxes)
        path = f"media/{oid}-{i + 1}.jpg"
        save(it["img"], path)
        item = {"src": path, "caption": m.get("caption", ""), "hash": it["hash"]}
        if it["kind"] == "frame":
            item.update({"from_video": True, "t": it["t"]})
        media.append(item)
        redactions.append((path, [b.get("what", "?") for b in boxes]))
    for k, c in enumerate(clips_plan):
        path = f"media/{oid}-clip-{k + 1}.mp4"
        make_clip(c["video"]["path"], c["start"], c["end"], path)
        media.append({"src": path, "type": "video", "caption": c["reason"], "start": c["start"], "end": c["end"]})

    e = res["entry"]
    for k, v in (res.get("new_patterns") or {}).items():
        if k not in patterns:
            patterns[k] = v
    entry = {
        "id": oid,
        "title": e.get("title", "Untitled"),
        "category": category or e.get("category", ""),
        "country": "KR",
        "service": e.get("service", ""),
        "place": place or "",
        "date": date or None,
        "scenario": e.get("scenario", ""),
        "access": e.get("access", {"requires": [], "unverified": []}),
        "media": media,
        "observed": e.get("observed", []),
        "confirmed": [],
        "interpretation": e.get("interpretation", []),
        "open": e.get("open", []),
        "sources": [],
        "related": [],
        "rights": "Own material. Personal data blurred automatically; reviewed before publication.",
        "breakdown": e.get("breakdown", {"steps": [], "elements": [], "patterns": []}),
        "submitted_via": f"issue #{issue['number']}",
    }
    d["observations"].append(entry)
    d["updated"] = datetime.date.today().isoformat()
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")

    os.makedirs(OUT, exist_ok=True)
    open(f"{OUT}/id", "w").write(oid)
    open(f"{OUT}/title", "w").write(entry["title"])
    lines = [f"Closes #{issue['number']}", "", f"## {entry['title']}", "", entry["scenario"], "",
             f"Category: {entry['category']} · Place: {entry['place'] or '—'} · Date: {entry['date'] or '—'}", ""]
    if dup_notes:
        lines += ["### ⚠ Possible duplicates", *[f"- {n}" for n in dup_notes], ""]
    if video_notes:
        lines += ["### Video", *[f"- {n}" for n in video_notes], "- Full videos are not stored.", ""]
    lines += ["### Blurred areas (check every image)"]
    for path, what in redactions:
        lines.append(f"- `{path}`: {', '.join(what) if what else 'nothing blurred'}")
    lines += ["", "### Check before merging",
              "- Nothing personal is readable in the images, the clip or the text.",
              "- `observed` holds only what is visible or what you said.",
              "- Korean text in the breakdown matches the images.",
              "", "Edit `data/observations.json` in this PR if something is wrong, then merge to publish."]
    open(f"{OUT}/pr.md", "w").write("\n".join(lines))
    open(f"{OUT}/summary.txt", "w").write(
        ("Possible duplicates: " + "; ".join(dup_notes) + ". " if dup_notes else "") + ("; ".join(video_notes) + "." if video_notes else ""))
    print(f"Entry {oid}: {entry['title']} ({len(items)} images, {len(clips_plan)} clips)")


if __name__ == "__main__":
    main()
