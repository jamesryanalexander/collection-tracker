(function () {
  var statusEl = document.querySelector("[data-sync-status]");
  if (!statusEl) return;

  function poll() {
    fetch("/sync/status")
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.status === "running") {
          statusEl.textContent = "Syncing… " + (data.items_seen || 0) + " items processed so far.";
          setTimeout(poll, 2000);
        } else if (data.status === "success") {
          statusEl.textContent =
            "Last sync: " + data.items_seen + " items (" + data.items_new + " new, " +
            data.items_updated + " updated, " + data.flags_raised + " flags raised).";
        } else if (data.status === "error") {
          statusEl.textContent = "Last sync failed: " + (data.message || "unknown error");
        }
      })
      .catch(function () {
        /* transient network error while polling -- next poll will retry */
      });
  }

  poll();
})();
