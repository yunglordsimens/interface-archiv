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
