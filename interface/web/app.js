async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function createCategoryIcon(kind) {
  const value = String(kind || "").toLowerCase();
  const category = /car|auto|vehicle|motor/.test(value) ? "auto"
    : /medical|health|dental|prescription/.test(value) ? "health"
      : /home|house|rent|property/.test(value) ? "home"
        : /license|licence|passport|identity|card/.test(value) ? "identity"
          : /subscription|membership|gym|fitness/.test(value) ? "subscription"
            : "calendar";
  const paths = {
    auto: ["M5 16l1.5-5h11L19 16", "M4 16h16v4H4z", "M7 20v1", "M17 20v1", "M7 16h.01", "M17 16h.01"],
    health: ["M20.8 8.8c0 5.2-8.8 10-8.8 10s-8.8-4.8-8.8-10A4.8 4.8 0 0 1 12 6.5a4.8 4.8 0 0 1 8.8 2.3Z", "M12 8v6", "M9 11h6"],
    home: ["m3 10 9-7 9 7", "M5 9v11h14V9", "M9 20v-6h6v6"],
    identity: ["M4 5h16v14H4z", "M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M6 15c.5-2 3.5-2 4 0", "M13 9h4", "M13 13h4", "M13 16h3"],
    subscription: ["M20 7v5h-5", "M4 17v-5h5", "M5.6 9A7 7 0 0 1 18 6l2 2", "M18.4 15A7 7 0 0 1 6 18l-2-2"],
    calendar: ["M5 5h14v16H5z", "M8 3v4", "M16 3v4", "M5 10h14", "M9 14h2", "M13 14h2", "M9 17h2"],
  }[category];
  const icon = document.createElement("span");
  icon.className = `upcoming-icon ${category}`;
  icon.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const d of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  icon.append(svg);
  return icon;
}

function formatRenewalDate(value) {
  if (!value) return "Renewal date not set";
  const date = new Date(`${value}T00:00:00Z`);
  return `Renews ${new Intl.DateTimeFormat(undefined, {
    day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
  }).format(date)}`;
}

function daysUntil(value) {
  if (!value) return "Date pending";
  const target = new Date(`${value}T00:00:00Z`);
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((target.getTime() - today) / 86400000);
  if (days < 0) return `${Math.abs(days)} ${Math.abs(days) === 1 ? "day" : "days"} overdue`;
  if (days === 0) return "Due today";
  return `In ${days} ${days === 1 ? "day" : "days"}`;
}

function render(listId, items, due = false) {
  const list = document.getElementById(listId);
  list.replaceChildren();
  document.getElementById(due ? "due-count" : "item-count").textContent = due
    ? `View all (${items.length}) →`
    : `${items.length} ${items.length === 1 ? "item" : "items"}`;
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = due ? "Nothing due soon." : "No items yet.";
    list.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("li");
    if (due) {
      row.className = "upcoming-row";
      const icon = createCategoryIcon(item.kind);
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = item.provider;
      const who = document.createElement("span");
      who.className = "who upcoming-detail";
      who.textContent = `${item.kind} · ${formatRenewalDate(item.renewal_date)}`;
      const copy = document.createElement("span");
      copy.className = "upcoming-copy";
      copy.append(what, who);
      const state = document.createElement("span");
      state.className = "state soon";
      state.textContent = daysUntil(item.renewal_date);
      row.append(icon, copy, state);
      list.append(row);
      continue;
    }
    row.className = `household-item ${item.state === "cancelled" ? "cancelled" : "active"}`;
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.setAttribute("aria-label", `${item.provider}, currently ${item.state}. Click to change status.`);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = item.renewal_date ? `renews ${item.renewal_date}` : "no date";
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = `${item.provider} · ${item.kind}`;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.holder || "";
    const status = document.createElement("span");
    status.className = `household-status ${item.state === "cancelled" ? "cancelled" : "active"}`;
    status.textContent = item.state === "cancelled" ? "Cancelled" : "Active";
    row.append(when, what, who, status);
    const changeStatus = async () => {
      if (row.classList.contains("updating")) return;
      row.classList.add("updating");
      row.setAttribute("aria-busy", "true");
      try {
        await request("/api/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            item_id: item.item_id,
            status: item.state === "cancelled" ? "active" : "cancelled",
          }),
        });
        await refresh();
      } catch (error) {
        message(error.message);
      } finally {
        row.classList.remove("updating");
        row.removeAttribute("aria-busy");
      }
    };
    row.addEventListener("click", changeStatus);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        changeStatus();
      }
    });
    list.append(row);
  }
}

function message(value) {
  document.getElementById("notice").textContent = value;
}

function badge(text, variant) {
  const span = document.createElement("span");
  span.className = `badge ${variant || ""}`.trim();
  span.textContent = text;
  return span;
}

function renderFindings(findings) {
  const list = document.getElementById("findings");
  list.replaceChildren();
  if (!findings || !findings.findings || !findings.findings.length) {
    document.getElementById("findings-count").textContent = "View all (0) →";
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Nothing needs attention.";
    list.append(empty);
    return;
  }
  document.getElementById("findings-count").textContent = `View all (${findings.findings.length}) →`;
  for (const finding of findings.findings) {
    const row = document.createElement("li");
    row.className = `finding ${finding.severity || ""}`.trim();
    const icon = document.createElement("span");
    icon.className = `finding-icon ${finding.kind || ""}`.trim();
    icon.setAttribute("aria-hidden", "true");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const paths = {
      deadline: ["M10.3 3.8 2.9 17a2 2 0 0 0 1.8 3h14.6a2 2 0 0 0 1.8-3L13.7 3.8a2 2 0 0 0-3.4 0Z", "M12 9v4", "M12 16.5h.01"],
      duplicate_charge: ["M8 4h10a2 2 0 0 1 2 2v13H8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z", "M6 7H4v12a2 2 0 0 0 2 2h12", "M10 9h5", "M10 13h6", "M10 17h3"],
      clear: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "m8.5 12 2.2 2.2 4.8-5"],
    }[finding.kind] || ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 11v5", "M12 8h.01"];
    for (const d of paths) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    icon.append(svg);
    const what = document.createElement("span");
    what.className = "what";
    const title = document.createElement("strong");
    title.textContent = finding.title;
    const detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent = finding.detail;
    what.append(title, detail);
    const arrow = document.createElement("a");
    arrow.className = "finding-arrow";
    arrow.href = finding.kind === "deadline" ? "#coming-up" : "#household";
    arrow.setAttribute("aria-label", finding.kind === "deadline"
      ? "View upcoming dates"
      : "View household items");
    arrow.textContent = "›";
    row.append(icon, what, arrow);
    list.append(row);
  }
}

function renderTrace(run) {
  const list = document.getElementById("trace");
  const label = document.getElementById("trace-run");
  list.replaceChildren();
  if (!run || !run.trace || !run.trace.length) {
    label.textContent = "";
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Process a document to see its steps.";
    list.append(empty);
    return;
  }
  label.textContent = `${run.trace.length} steps · ${run.status}`;
  const taskLabels = {
    Redactor: "Sensitive data redacted",
    Extractor: "Document parsed",
    Verifier: "Entities identified",
    Researcher: "Relevant information checked",
    Drafter: "Suggestions generated",
    Auditor: "Suggestions reviewed",
    Orchestrator: "Document processed",
  };
  for (const step of run.trace) {
    const row = document.createElement("li");
    row.className = `trace-step${step.status === "ok" ? " is-complete" : " is-error"}`;
    const check = document.createElement("span");
    check.className = "trace-check";
    check.setAttribute("aria-hidden", "true");
    check.textContent = step.status === "ok" ? "✓" : "!";
    const task = document.createElement("span");
    task.className = "trace-task";
    task.textContent = taskLabels[step.agent] || "Document processed";
    row.append(check, task);
    list.append(row);
  }
}

async function refreshTrace() {
  try {
    const data = await request("/api/trace");
    renderTrace(data.run);
  } catch (error) {
    // A trace is evidence, not a requirement for the household list.
    console.warn(error.message);
  }
}

function addChatMessage(role, text) {
  const log = document.getElementById("chat-log");
  const bubble = document.createElement("div");
  bubble.className = `chat-message ${role}`;
  bubble.textContent = text;
  if (role === "assistant") {
    const row = document.createElement("div");
    row.className = "chat-row assistant-row";
    const avatar = document.createElement("span");
    avatar.className = "chat-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.append(document.createElement("span"));
    row.append(avatar, bubble);
    log.append(row);
  } else {
    log.append(bubble);
  }
  log.scrollTop = log.scrollHeight;
}

async function sendChat(text) {
  addChatMessage("user", text);
  const input = document.getElementById("chat-input");
  const button = document.querySelector("#chat-form button[type=submit]");
  button.disabled = true;
  try {
    const data = await request("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    addChatMessage("assistant", data.reply);
    await refresh();
  } catch (error) {
    addChatMessage("assistant", error.message);
  } finally {
    button.disabled = false;
    input.value = "";
    input.focus();
  }
}

document.getElementById("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = document.getElementById("chat-input");
  if (input.value.trim()) sendChat(input.value.trim());
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => sendChat(button.dataset.prompt));
});

async function refresh() {
  const data = await request("/api/summary");
  renderFindings(data.findings);
  render("due", data.due, true);
  render("items", data.items);
}

const fileInput = document.getElementById("file");
const uploadDropzone = document.getElementById("upload-dropzone");
const fileName = document.getElementById("file-name");

fileInput.addEventListener("change", () => {
  fileName.textContent = fileInput.files[0]?.name || "PDF, image, text, or email · max 15 MB";
  uploadDropzone.classList.toggle("has-file", Boolean(fileInput.files[0]));
});

for (const eventName of ["dragenter", "dragover"]) {
  uploadDropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    uploadDropzone.classList.add("is-dragging");
  });
}

uploadDropzone.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget || !uploadDropzone.contains(event.relatedTarget)) {
    uploadDropzone.classList.remove("is-dragging");
  }
});

uploadDropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  uploadDropzone.classList.remove("is-dragging");
  const droppedFile = event.dataTransfer.files[0];
  if (!droppedFile) return;
  const transfer = new DataTransfer();
  transfer.items.add(droppedFile);
  fileInput.files = transfer.files;
  fileInput.dispatchEvent(new Event("change", { bubbles: true }));
});

document.getElementById("upload").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  const file = document.getElementById("file").files[0];
  if (!file) return;
  submit.disabled = true;
  message("Reading and redacting locally…");
  try {
    const result = await request("/api/upload", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });
    const draft = document.getElementById("draft");
    draft.hidden = false;
    draft.replaceChildren();
    const heading = document.createElement("h2");
    heading.textContent = `Draft for ${result.item.provider} · ${result.deadline}`;
    const body = document.createElement("pre");
    body.textContent = result.draft;
    draft.append(heading, body);
    if (result.audit) {
      const audit = document.createElement("p");
      audit.className = "notice";
      audit.textContent = result.status === "ok"
        ? `Auditor approved after ${result.retries} revision(s).`
        : `Auditor stopped at ${result.status}: ${result.audit.reasons.join("; ")}`;
      draft.append(audit);
    }
    if (result.trace) renderTrace({ trace: result.trace, status: result.status });
    message("Notice processed. Review the draft before sending it yourself.");
    await refresh();
  } catch (error) {
    message(error.message);
  } finally {
    submit.disabled = false;
  }
});

refresh().catch((error) => message(error.message));
refreshTrace();
