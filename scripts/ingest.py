"""Turn a "New observation" issue into a draft archive entry.

Reads the issue, downloads its images, asks Gemini for a structured entry and
for boxes around personal data, blurs those boxes, compresses the images and
appends the entry to data/observations.json. The workflow then opens a PR.

Env: GEMINI_API_KEY, GEMINI_MODEL (optional), GH_TOKEN, ISSUE_NUMBER, REPO.
Local test: MOCK_RESPONSE=path/to/response.json skips the Gemini call.
"""
import base64
import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

from PIL import Image, ImageFilter, ImageOps

DATA = "data/observations.json"
OUT = ".ingest"
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash"] if m]


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=180) as r:
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


def download(url):
    headers = {"User-Agent": "interface-archive-ingest"}
    if "github.com" in url or "githubusercontent.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    try:
        raw = http(url, headers=headers)
    except urllib.error.HTTPError:
        raw = http(url, headers={"User-Agent": "interface-archive-ingest"})
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    return img.convert("RGB")


def for_model(img):
    im = img.copy()
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


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
- Steps only if the images show a sequence; otherwise an empty list.
- Patterns: reuse an existing id when it fits (list below). Add a new id to new_patterns only for a clearly recurring solution. Ids are lowercase-with-hyphens.
- Never copy personal data into any text field: phone numbers of private people, personal names, home or delivery addresses,
  tracking or order numbers, card or account numbers. Write "(blurred)" instead.
- media.redact: boxes around every piece of personal data visible in each image: private phone numbers, personal names,
  addresses, tracking/order/booking numbers, card numbers, faces of people, licence plates, QR codes or barcodes tied to a person,
  parcel labels, chat messages from private people. Do NOT blur public business information (store names, prices,
  customer service numbers, product names). box_2d uses coordinates normalised to 0-1000. Pad boxes slightly. If unsure, include it.
- One media item per image, same order as given, index starting at 0.
"""


def call_gemini(images, context, patterns):
    if os.environ.get("MOCK_RESPONSE"):
        return json.load(open(os.environ["MOCK_RESPONSE"]))
    text = PROMPT + "\nExisting patterns:\n" + json.dumps(
        {k: v["label"] for k, v in patterns.items()}, ensure_ascii=False) + "\n\nAuthor's context:\n" + context
    parts = [{"text": text}] + [{"inline_data": {"mime_type": "image/jpeg", "data": for_model(i)}} for i in images]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            res = json.loads(http(url, body, {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]}))
        except urllib.error.HTTPError as e:
            last = f"{model}: HTTP {e.code} {e.read()[:300]!r}"
            print(last, file=sys.stderr)
            if e.code == 404:
                continue
            raise SystemExit(last)
        txt = res["candidates"][0]["content"]["parts"][0]["text"]
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
        usage = res.get("usageMetadata", {})
        print(f"model={model} tokens in={usage.get('promptTokenCount')} out={usage.get('candidatesTokenCount')}")
        return json.loads(txt)
    raise SystemExit(f"No Gemini model available. Last error: {last}")


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


def main():
    issue = gh(f"issues/{os.environ['ISSUE_NUMBER']}")
    s = sections(issue["body"])
    photos = s.get("Photos or screenshots", issue["body"] or "")
    urls = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", photos) + re.findall(r'<img[^>]+src="(https?://[^"]+)"', photos)
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise SystemExit("No images found in the issue. Drag photos into the first field.")
    images = [download(u) for u in urls]

    context = clean(s.get("What happened", ""))
    place = clean(s.get("Place", ""))
    date = clean(s.get("Date", ""))
    category = clean(s.get("Category", ""))

    d = json.load(open(DATA, encoding="utf-8"))
    patterns = d.setdefault("patterns", {})
    ctx = f"What happened: {context}\nPlace: {place or 'not given'}\nDate: {date or 'not given'}\nCategory chosen: {category or 'not given'}"
    res = scrub(call_gemini(images, ctx, patterns))

    ids = [int(o["id"]) for o in d["observations"] if str(o["id"]).isdigit()]
    oid = f"{(max(ids) + 1 if ids else 1):03d}"
    os.makedirs("media", exist_ok=True)
    media, redactions = [], []
    by_index = {m.get("index", i): m for i, m in enumerate(res.get("media", []))}
    for i, img in enumerate(images):
        m = by_index.get(i, {})
        boxes = m.get("redact", []) or []
        redact(img, boxes)
        path = f"media/{oid}-{i + 1}.jpg"
        save(img, path)
        media.append({"src": path, "caption": m.get("caption", "")})
        redactions.append((path, [b.get("what", "?") for b in boxes]))

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
        "rights": "Own photo. Personal data blurred automatically; reviewed before publication.",
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
             f"Category: {entry['category']} · Place: {entry['place'] or '—'} · Date: {entry['date'] or '—'}", "",
             "### Blurred areas (check every image)"]
    for path, what in redactions:
        lines.append(f"- `{path}`: {', '.join(what) if what else 'nothing blurred'}")
    lines += ["", "### Check before merging",
              "- Nothing personal is readable in the images or the text.",
              "- `observed` holds only what is visible or what you said.",
              "- Korean text in the breakdown matches the images.",
              "", "Edit `data/observations.json` in this PR if something is wrong, then merge to publish."]
    open(f"{OUT}/pr.md", "w").write("\n".join(lines))
    print(f"Entry {oid}: {entry['title']} ({len(images)} images)")


if __name__ == "__main__":
    main()
