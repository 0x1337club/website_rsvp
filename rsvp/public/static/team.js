(function () {
  "use strict";

  var token = location.pathname.split("/").filter(Boolean).pop();
  var api = "/api/team/" + encodeURIComponent(token);
  var members = [];
  var confirmed = []; // last state the server confirmed; failed saves fall back to it
  var saving = Promise.resolve();

  var $ = function (id) { return document.getElementById(id); };

  // Parse a JSON response, tolerating HTML error pages from proxies or crashes.
  function readJSON(res) {
    return res.text().then(function (text) {
      try { return JSON.parse(text); } catch (e) {
        return { error: "The server sent an unexpected response (HTTP " + res.status + "). Please try again." };
      }
    });
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "className") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function status(msg, isError) {
    var bar = $("savebar");
    bar.textContent = msg;
    bar.className = "savebar" + (isError ? " err" : "");
  }

  function renderTally() {
    var yes = 0, no = 0;
    members.forEach(function (m) { if (m.status === "yes") yes++; else if (m.status === "no") no++; });
    var pending = members.length - yes - no;
    var tally = $("tally");
    tally.textContent = "";
    tally.appendChild(el("span", { className: "pill yes", text: yes + " going" }));
    tally.appendChild(el("span", { className: "pill no", text: no + " not going" }));
    if (pending) tally.appendChild(el("span", { className: "pill pending", text: pending + " yet to reply" }));
  }

  function renderMembers() {
    var list = $("members");
    list.textContent = "";
    members.forEach(function (m) {
      var label = m.name || m.email;
      var who = el("div", { className: "who" }, [
        el("strong", { text: label }),
        el("small", { text: m.name ? m.email : "Invited, hasn't registered yet" }),
      ]);
      var seg = el("div", { className: "seg", role: "group", "aria-label": "RSVP for " + label }, [
        el("button", { type: "button", className: "yes", "aria-pressed": String(m.status === "yes"), "data-id": m.id, "data-status": "yes", text: "Going" }),
        el("button", { type: "button", className: "no", "aria-pressed": String(m.status === "no"), "data-id": m.id, "data-status": "no", text: "Not going" }),
      ]);
      list.appendChild(el("li", { className: "member" }, [who, seg]));
    });
    renderTally();
  }

  function copy(list) { return list.map(function (m) { return Object.assign({}, m); }); }

  function applyServer(list) {
    confirmed = copy(list);
    members = copy(list);
    renderMembers();
  }

  function save(updates) {
    updates.forEach(function (u) {
      members.forEach(function (m) { if (m.id === u.id) m.status = u.status; });
    });
    renderMembers();
    status("Saving…");

    saving = saving.then(function () {
      return fetch(api + "/rsvp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: updates }),
      }).then(function (res) {
        return readJSON(res).then(function (data) {
          if (!res.ok) throw new Error(data.error || "Couldn't save.");
          applyServer(data.members);
          status("Saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
        });
      }).catch(function (err) {
        // Show what's actually saved, then quietly re-check in case someone else changed it.
        members = copy(confirmed);
        renderMembers();
        fetch(api).then(readJSON).then(function (d) { if (d && d.members) applyServer(d.members); }).catch(function () {});
        var msg = err && err.message && err.message !== "Failed to fetch" ? err.message : "Couldn't reach the server.";
        status("Not saved: " + msg + " Check your connection and try again.", true);
      });
    });
  }

  $("members").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-id]");
    if (!btn) return;
    var id = Number(btn.getAttribute("data-id"));
    var next = btn.getAttribute("data-status");
    var current = members.filter(function (m) { return m.id === id; })[0];
    if (current && current.status === next) return;
    save([{ id: id, status: next }]);
  });

  function setAll(next) {
    var updates = members
      .filter(function (m) { return m.status !== next; })
      .map(function (m) { return { id: m.id, status: next }; });
    if (updates.length) save(updates);
  }
  $("all-yes").addEventListener("click", function () { setAll("yes"); });
  $("all-no").addEventListener("click", function () { setAll("no"); });

  fetch(api).then(function (res) {
    return readJSON(res).then(function (data) {
      if (!res.ok) throw new Error(data.error || "Couldn't load this team.");
      document.title = data.team + " · RSVP";
      $("event").textContent = data.event;
      $("team").textContent = data.team;
      applyServer(data.members);
    });
  }).catch(function (err) {
    $("team").textContent = "Couldn't load your team";
    $("lede").textContent = (err.message && err.message !== "Failed to fetch" ? err.message + " " : "") +
      "Check your connection and refresh the page.";
  });
})();
