// Shared Supabase client and helpers for Add and Inbox.
// The anon key is public by design; access is enforced by row-level security in the database.
window.ARCHIVE = {
  url: "https://oyuwwnydlxoxpwkaxtdh.supabase.co",
  key: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im95dXd3bnlkbHhveHB3a2F4dGRoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTMyNTQ3MjksImV4cCI6MjEwODgzMDcyOX0.S94wjbi2qNRkXsXHyuzK6oM-d_vJqIC8lTe0WyxhPd4",
  bucket: "inbox",
  owner: "marushach@gmail.com"
};
window.sb = window.supabase.createClient(ARCHIVE.url, ARCHIVE.key, { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } });

window.esc = function (s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
};
window.uuid = function () {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, function (c) {
    return (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16);
  });
};
window.kindOf = function (file) {
  var t = (file.type || "").toLowerCase();
  if (t.indexOf("video/") === 0) return "video";
  if (t.indexOf("audio/") === 0) return "audio";
  return "image";
};
window.extOf = function (file) {
  var m = /\.([a-z0-9]+)$/i.exec(file.name || "");
  if (m) return m[1].toLowerCase();
  var t = (file.type || "").split("/")[1] || "bin";
  return t.replace("quicktime", "mov").replace("jpeg", "jpg").split(";")[0];
};
window.toast = function (msg, ms) {
  var el = document.createElement("div");
  el.className = "toast"; el.textContent = msg; el.setAttribute("role", "status");
  document.body.appendChild(el);
  setTimeout(function () { el.remove(); }, ms || 3000);
};
window.isOwner = async function () {
  var r = await sb.auth.getSession();
  var s = r.data && r.data.session;
  return !!(s && s.user && (s.user.email || "").toLowerCase() === ARCHIVE.owner);
};

// Voice note recorder (MediaRecorder). Returns a controller; onDone(blob) receives the recording.
window.makeRecorder = function (onDone) {
  var rec = null, chunks = [];
  return {
    supported: !!(navigator.mediaDevices && window.MediaRecorder),
    start: async function () {
      var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      var type = MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : (MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "");
      rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      chunks = [];
      rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        onDone(new Blob(chunks, { type: rec.mimeType || type || "audio/webm" }));
      };
      rec.start();
    },
    stop: function () { if (rec && rec.state !== "inactive") rec.stop(); }
  };
};

// Capture time from EXIF/XMP (photos, screenshots). Falls back to null; callers use file dates.
window.captureTime = async function (input) {
  if (!window.exifr) return null;
  try {
    var m = await exifr.parse(input, { tiff: true, exif: true, xmp: true, icc: false, iptc: false, mergeOutput: true });
    if (!m) return null;
    var d = m.DateTimeOriginal || m.CreateDate || m.DateCreated || m.DateTimeDigitized || m.ModifyDate || m.DateTime;
    if (typeof d === "string") d = new Date(d.replace(/^(\d{4}):(\d\d):(\d\d)/, "$1-$2-$3"));
    return d instanceof Date && !isNaN(d) ? d : null;
  } catch (e) { return null; }
};

// 64-bit difference hash of a loaded <img>, as 16 hex chars. Needs a CORS-clean image.
window.dhashImg = function (img) {
  try {
    var mid = document.createElement("canvas"); mid.width = 72; mid.height = 64;
    var mx = mid.getContext("2d"); mx.imageSmoothingQuality = "high"; mx.drawImage(img, 0, 0, 72, 64);
    var c = document.createElement("canvas"); c.width = 9; c.height = 8;
    var x = c.getContext("2d", { willReadFrequently: true });
    x.imageSmoothingQuality = "high"; x.drawImage(mid, 0, 0, 9, 8);
    var p = x.getImageData(0, 0, 9, 8).data, g = [], bits = "";
    for (var i = 0; i < p.length; i += 4) g.push(0.299 * p[i] + 0.587 * p[i + 1] + 0.114 * p[i + 2]);
    for (var r = 0; r < 8; r++) for (var col = 0; col < 8; col++) bits += g[r * 9 + col] > g[r * 9 + col + 1] ? "1" : "0";
    var hex = "";
    for (var k = 0; k < 64; k += 4) hex += parseInt(bits.slice(k, k + 4), 2).toString(16);
    return hex;
  } catch (e) { return null; }
};
window.hamming = function (a, b) {
  var d = 0;
  for (var i = 0; i < 16; i++) { var v = parseInt(a[i], 16) ^ parseInt(b[i], 16); while (v) { d += v & 1; v >>= 1; } }
  return d;
};
