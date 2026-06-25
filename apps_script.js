// ──────────────────────────────────────────────────────────────────
// Paste this into Google Form → Extensions → Apps Script
// Then set trigger: onFormSubmit → On form submit
// ──────────────────────────────────────────────────────────────────

const WEBHOOK_URL = "https://dollop-dazzling-unsuited.ngrok-free.dev/form-submit";

function onFormSubmit(e) {
  const responses = e.response.getItemResponses();
  const data = {};

  responses.forEach(function(r) {
    data[r.getItem().getTitle()] = r.getResponse();
  });

  const options = {
    method: "POST",
    contentType: "application/json",
    payload: JSON.stringify(data),
    muteHttpExceptions: true
  };

  try {
    const result = UrlFetchApp.fetch(WEBHOOK_URL, options);
    Logger.log("Webhook sent: " + result.getContentText());
  } catch (err) {
    Logger.log("Webhook error: " + err.toString());
  }
}
