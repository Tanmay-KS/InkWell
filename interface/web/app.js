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
  render("due", data.due, true);
  render("items", data.items);
}

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
    message("Notice processed. Review the draft before sending it yourself.");
    await refresh();
  } catch (error) {
    message(error.message);
  } finally {
    submit.disabled = false;
  }
});

refresh().catch((error) => message(error.message));
