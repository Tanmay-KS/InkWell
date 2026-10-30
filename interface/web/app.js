async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function render(listId, items, due = false) {
  const list = document.getElementById(listId);
  list.replaceChildren();
  document.getElementById(due ? "due-count" : "item-count").textContent =
    `${items.length} ${items.length === 1 ? "item" : "items"}`;
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = due ? "Nothing due in the next 30 days." : "Nothing tracked yet.";
    list.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("li");
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = due ? item.renewal_date : (item.renewal_date ? `renews ${item.renewal_date}` : "no date");
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = `${item.provider} · ${item.kind}`;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.holder || "";
    const state = document.createElement("span");
    state.className = `state ${item.state === "cancelled" ? "done" : "soon"}`;
    state.textContent = item.state;
    row.append(when, what, who, state);
    if (!due) {
      const action = document.createElement("button");
      action.type = "button";
      action.textContent = item.state === "cancelled" ? "Mark active" : "Mark cancelled";
      action.addEventListener("click", async () => {
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
        }
      });
      row.append(action);
      const id = document.createElement("code");
      id.textContent = item.item_id;
      row.append(id);
    }
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
  const summary = document.getElementById("findings-summary");
  list.replaceChildren();
  if (!findings || !findings.findings || !findings.findings.length) {
    summary.textContent = "No pushed findings yet.";
    document.getElementById("findings-count").textContent = "0 findings";
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Nothing needs attention this week.";
    list.append(empty);
    return;
  }
  document.getElementById("findings-count").textContent =
    `${findings.findings.length} ${findings.findings.length === 1 ? "finding" : "findings"}`;
  summary.textContent = findings.summary || "";
  for (const finding of findings.findings) {
    const row = document.createElement("li");
    row.className = `finding ${finding.severity || ""}`.trim();
    const what = document.createElement("span");
    what.className = "what";
    const title = document.createElement("strong");
    title.textContent = finding.title;
    const detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent = finding.detail;
    what.append(title, detail);
    row.append(what);
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
    empty.textContent = "Run a document to see every step.";
    list.append(empty);
    return;
  }
  label.textContent = `${run.trace.length} steps · ${run.status}`;
  for (const step of run.trace) {
    const row = document.createElement("li");
    const number = document.createElement("span");
    number.className = "step-no";
    number.textContent = step.step;
    const agent = document.createElement("span");
    agent.className = "step-agent";
    agent.textContent = step.agent;
    const summary = document.createElement("span");
    summary.className = "step-summary";
    const details = [step.output_summary];
    if (step.model) details.push(step.model);
    details.push(`${step.latency_ms} ms`);
    summary.textContent = details.filter(Boolean).join(" · ");
    const tier = badge(step.tier || "code", "tier");
    const egress = badge(
      step.egress === "none" ? "no egress" : step.egress,
      step.egress.startsWith("allowed") ? "bad" : "ok",
    );
    const status = badge(
      step.status,
      step.status === "ok" ? "ok" : "bad",
    );
    row.append(number, agent, summary, tier, egress, status);
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
  log.append(bubble);
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
  document.getElementById("mode").textContent =
    data.demo ? "Demo · fictional data" : "Local household";
  renderFindings(data.findings);
  render("due", data.due, true);
  render("items", data.items);
}

const fileInput = document.getElementById("file");
const uploadDropzone = document.getElementById("upload-dropzone");
const fileName = document.getElementById("file-name");

fileInput.addEventListener("change", () => {
  fileName.textContent = fileInput.files[0]?.name || "PDF, image, text, or email · up to 15 MB";
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
