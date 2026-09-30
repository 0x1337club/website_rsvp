(function () {
  "use strict";

  var data = null;
  var open = {};
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

  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { t.classList.remove("show"); }, 1800);
  }

  // Fallback for browsers that block the async clipboard API.
  function legacyCopy(text) {
    var area = el("textarea", { readonly: "", "aria-hidden": "true" });
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }

  function when(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function pct(n, total) {
    if (!total || !n) return "0%";
    var p = Math.round((n / total) * 100);
    return p === 0 ? "<1%" : p === 100 && n < total ? ">99%" : p + "%";
  }

  function showLogin() {
    $("dash").classList.add("hidden");
    $("login").classList.remove("hidden");
    $("pw").focus();
  }

  function load() {
    return fetch("/api/admin/summary", { credentials: "same-origin" }).then(function (res) {
      if (res.status === 401) { showLogin(); return false; }
      return readJSON(res).then(function (d) {
        if (!res.ok) throw new Error(d.error || "Couldn't load.");
        data = d;
        $("login").classList.add("hidden");
        $("dash").classList.remove("hidden");
        render();
        return true;
      });
    }).catch(function (err) {
      toast(err.message && err.message !== "Failed to fetch" ? err.message : "Couldn't reach the server. Check your connection.");
      return false;
    });
  }

  function stat(cls, label, value, sub) {
    return el("div", { className: "card stat " + cls }, [
      el("div", { className: "label", text: label }),
      el("div", { className: "value", text: String(value) }),
      sub ? el("div", { className: "sub", text: sub }) : null,
    ]);
  }

  function renderStats() {
    var t = data.totals;
    $("event").textContent = data.event;
    $("updated").textContent = "Updated " + when(data.generated_at);
    var stats = $("stats");
    stats.textContent = "";
    stats.appendChild(stat("", "People", t.people, t.teams + " teams"));
    stats.appendChild(stat("yes", "Going", t.yes, pct(t.yes, t.people) + " of everyone"));
    stats.appendChild(stat("no", "Not going", t.no, pct(t.no, t.people) + " of everyone"));
    stats.appendChild(stat("pending", "No reply", t.pending, pct(t.pending, t.people) + " of everyone"));
    stats.appendChild(stat("", "Teams done", t.teams_complete, "of " + t.teams + " fully replied"));

    var bar = $("bar");
    bar.textContent = "";
    ["yes", "no", "pending"].forEach(function (k) {
      var i = el("i", { className: k });
      i.style.width = t.people ? (t[k] / t.people) * 100 + "%" : "0";
      bar.appendChild(i);
    });
  }

  function matches(team, q, filter) {
    if (filter === "waiting" && !team.pending) return false;
    if (filter === "complete" && team.pending) return false;
    if (filter === "silent" && team.pending !== team.members.length) return false;
    if (filter === "has-no" && !team.no) return false;
    if (filter === "all-yes" && team.yes !== team.members.length) return false;
    if (!q) return true;
    if (team.name.toLowerCase().indexOf(q) !== -1) return true;
    return team.members.some(function (m) {
      return m.name.toLowerCase().indexOf(q) !== -1 || m.email.toLowerCase().indexOf(q) !== -1;
    });
  }

  var sorters = {
    name: function (a, b) { return a.name.toLowerCase().localeCompare(b.name.toLowerCase()); },
    pending: function (a, b) { return b.pending - a.pending || sorters.name(a, b); },
    yes: function (a, b) { return b.yes - a.yes || sorters.name(a, b); },
    recent: function (a, b) { return (b.updated_at || "").localeCompare(a.updated_at || "") || sorters.name(a, b); },
  };

  function statusLabel(s) {
    if (s === "yes") return el("span", { className: "pill yes", text: "Going" });
    if (s === "no") return el("span", { className: "pill no", text: "Not going" });
    return el("span", { className: "pill pending", text: "No reply" });
  }

  function renderRows() {
    var q = $("q").value.trim().toLowerCase();
    var filter = $("filter").value;
    var teams = data.teams.filter(function (t) { return matches(t, q, filter); }).sort(sorters[$("sort").value]);
    var people = teams.reduce(function (n, t) { return n + t.members.length; }, 0);
    $("shown").textContent = "Showing " + teams.length + " of " + data.teams.length + " teams (" + people + " people)";

    var body = $("rows");
    body.textContent = "";
    if (!teams.length) {
      body.appendChild(el("tr", {}, [el("td", { colspan: "7", className: "muted", text: "No teams match." })]));
      return;
    }
    teams.forEach(function (t) {
      var isOpen = !!open[t.name] || (q && !t.name.toLowerCase().includes(q));
      var row = el("tr", { className: "team" + (isOpen ? " open" : ""), "data-team": t.name }, [
        el("td", { className: "tname" }, [el("span", { className: "caret", text: "›" }), document.createTextNode(t.name)]),
        el("td", { className: "num hide-sm", text: String(t.members.length) }),
        el("td", { className: "num yes", text: String(t.yes) }),
        el("td", { className: "num no", text: String(t.no) }),
        el("td", { className: "num pending", text: String(t.pending) }),
        el("td", { className: "muted hide-sm", text: when(t.updated_at) }),
        el("td", { className: "links" }, [
          el("button", { type: "button", className: "btn small", "data-copy": t.link, text: "Copy link" }),
          el("a", { className: "btn small hide-sm", href: t.link, target: "_blank", rel: "noopener noreferrer", text: "Open" }),
        ]),
      ]);
      body.appendChild(row);
      if (!isOpen) return;
      var sub = el("table", { className: "sub" }, t.members.map(function (m) {
        return el("tr", {}, [
          el("td", { text: m.name || "(invited, not registered)" }),
          el("td", { className: "muted", text: m.email }),
          el("td", {}, [statusLabel(m.status)]),
          el("td", { className: "muted hide-sm", text: when(m.updated_at) }),
        ]);
      }));
      body.appendChild(el("tr", { className: "detail" }, [el("td", { colspan: "7" }, [sub])]));
    });
  }

  function render() {
    $("empty").classList.toggle("hidden", data.totals.teams > 0);
    renderStats();
    renderRows();
  }

  function notice(msg, isError) {
    var n = $("import-result");
    n.textContent = msg;
    n.className = "notice" + (isError ? " err" : "");
  }

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  // Upload files one at a time so each result (or error) is reported against its file.
  function importFiles(files) {
    var lines = [];
    var failed = false;
    notice("Importing " + plural(files.length, "file") + "…");
    return files.reduce(function (chain, file) {
      return chain.then(function () {
        return file.text().then(function (text) {
          return fetch("/api/admin/import", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "text/csv" },
            body: text,
          });
        }).then(function (res) {
          return readJSON(res).then(function (r) {
            if (res.status === 401) throw new Error("Your session expired. Sign in again.");
            if (!res.ok) throw new Error(r.error || "Import failed.");
            lines.push(file.name + ": " + plural(r.rows, "row") + ", " + (r.new_people === 1 ? "1 new person" : r.new_people + " new people") +
              ", " + plural(r.new_teams, "new team") + (r.moved ? ", " + r.moved + " moved team" : "") + ".");
          });
        }).catch(function (err) {
          failed = true;
          lines.push(file.name + ": " + (err.message && err.message !== "Failed to fetch" ? err.message : "Couldn't reach the server."));
        });
      });
    }, Promise.resolve()).then(function () {
      notice(lines.join(" "), failed);
      return load();
    });
  }

  $("import-file").addEventListener("change", function (e) {
    var files = Array.prototype.slice.call(e.target.files || []);
    e.target.value = "";
    if (files.length) importFiles(files);
  });

  $("rows").addEventListener("click", function (e) {
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var link = copy.getAttribute("data-copy");
      (navigator.clipboard ? navigator.clipboard.writeText(link) : Promise.reject())
        .catch(function () { if (!legacyCopy(link)) throw new Error(); })
        .then(function () { toast("Link copied"); }, function () { window.prompt("Copy this link:", link); });
      return;
    }
    if (e.target.closest("a")) return;
    var row = e.target.closest("tr.team");
    if (!row) return;
    var name = row.getAttribute("data-team");
    open[name] = !open[name];
    renderRows();
  });

  ["q", "filter", "sort"].forEach(function (id) {
    $(id).addEventListener(id === "q" ? "input" : "change", function () { if (data) renderRows(); });
  });

  $("refresh").addEventListener("click", function () { load().then(function (ok) { if (ok) toast("Refreshed"); }); });

  $("logout").addEventListener("click", function () {
    fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" }).then(function (res) {
      if (!res.ok) throw new Error();
      data = null;
      showLogin();
    }).catch(function () {
      toast("Couldn't reach the server to log out. Check your connection and try again.");
    });
  });

  $("login").addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = e.target.querySelector("button");
    btn.disabled = true;
    $("login-msg").textContent = "";
    fetch("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("pw").value }),
    }).then(function (res) {
      return readJSON(res).then(function (d) {
        if (!res.ok) throw new Error(d.error || "Couldn't sign in.");
        $("pw").value = "";
        return load();
      });
    }).catch(function (err) {
      $("login-msg").textContent = err.message && err.message !== "Failed to fetch" ? err.message : "Couldn't reach the server.";
    }).then(function () { btn.disabled = false; });
  });

  setInterval(function () { if (data && !document.hidden) load(); }, 30000);
  load();
})();
