/**
 * Google Drive → archive inbox.
 *
 * Put photos, screenshots and videos into one Google Drive folder (the Drive app on the phone
 * keeps uploading in the background). Every 10 minutes this script moves new files from that
 * folder into the private archive inbox, where they appear under "New".
 *
 * Setup (once), in script.google.com → New project → paste this file:
 *   1. Project Settings (gear) → Script properties → add:
 *        SUPABASE_URL          https://oyuwwnydlxoxpwkaxtdh.supabase.co
 *        SUPABASE_SERVICE_KEY  the same secret key you put into GitHub
 *        FOLDER_ID             the id of your Drive folder (the part after /folders/ in its link)
 *   2. Choose the function "setup" at the top and press Run. Allow access when Google asks.
 * After that it runs by itself. Imported files are moved into the subfolder "Imported".
 */

var MAX_BYTES = 49 * 1024 * 1024;   // storage limit per file is 50 MB
var MAX_RUN_MS = 5 * 60 * 1000;     // Apps Script stops scripts after 6 minutes

function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "importNow") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("importNow").timeBased().everyMinutes(10).create();
  importNow();
}

function importNow() {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty("SUPABASE_URL").replace(/\/$/, "");
  var key = props.getProperty("SUPABASE_SERVICE_KEY");
  var folder = DriveApp.getFolderById(props.getProperty("FOLDER_ID"));
  var done = subfolder_(folder, "Imported");
  var tooBig = subfolder_(folder, "Too big (trim and add again)");
  var started = Date.now(), count = 0;

  var files = folder.getFiles();
  while (files.hasNext() && Date.now() - started < MAX_RUN_MS) {
    var file = files.next();
    var mime = file.getMimeType() || "";
    var kind = mime.indexOf("video/") === 0 ? "video" : mime.indexOf("audio/") === 0 ? "audio" : mime.indexOf("image/") === 0 ? "image" : null;
    if (!kind) continue;
    if (file.getSize() > MAX_BYTES) { file.moveTo(tooBig); continue; }

    var id = Utilities.getUuid();
    var ext = (file.getName().match(/\.([A-Za-z0-9]+)$/) || [null, mime.split("/")[1] || "bin"])[1].toLowerCase();
    var day = Utilities.formatDate(file.getDateCreated(), "Asia/Seoul", "yyyy-MM-dd");
    var path = "owner/" + day + "/" + id + "." + ext;

    var up = UrlFetchApp.fetch(url + "/storage/v1/object/inbox/" + path, {
      method: "post", contentType: mime, payload: file.getBlob().getBytes(),
      headers: headers_(key), muteHttpExceptions: true
    });
    if (up.getResponseCode() >= 300) { console.error("upload failed", file.getName(), up.getContentText()); continue; }

    var row = UrlFetchApp.fetch(url + "/rest/v1/uploads", {
      method: "post", contentType: "application/json",
      headers: Object.assign(headers_(key), { Prefer: "return=minimal" }),
      payload: JSON.stringify({
        id: id, path: path, kind: kind, mime: mime, size: file.getSize(), guest: false, status: "new",
        note: "", taken_at: file.getDateCreated().toISOString()
      }),
      muteHttpExceptions: true
    });
    if (row.getResponseCode() >= 300) { console.error("row failed", file.getName(), row.getContentText()); continue; }
    file.moveTo(done);
    count++;
  }
  console.log("imported " + count);
}

function headers_(key) {
  var h = { apikey: key };
  if (key.indexOf("eyJ") === 0) h.Authorization = "Bearer " + key;
  return h;
}

function subfolder_(parent, name) {
  var it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}
