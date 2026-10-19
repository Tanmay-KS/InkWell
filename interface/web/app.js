// Ledger — simple web view.
//
// Phase 4 — Person B.
//
// The view is deliberately thin. It renders two lists and posts one form.
// All the logic lives in the agent pipeline; this file's job is to show
// what the pipeline knows.
//
// Phase 4 will replace the inline fixture with a fetch() against the
// gateway's OpenAI-compatible API server, exposed through OpenShell with
// `openshell service expose` so the demo URL never punches a hole in the
// host firewall.

const FIXTURE = {
  due: [
    { when: "in 3 days", what: "Car insurance renewal", who: "Sam", state: "soon" },
    { when: "in 12 days", what: "Gym membership", who: "Sam", state: "soon" },
    { when: "in 26 days", what: "Dental plan renewal", who: "Priya", state: "soon" },
  ],
  items: [
    { when: "renews 3 Nov", what: "Car insurance", who: "Sam", state: "soon" },
    { when: "renews 12 Nov", what: "Gym membership", who: "Sam", state: "soon" },
    { when: "renews 26 Nov", what: "Dental plan", who: "Priya", state: "soon" },
    { when: "cancelled 4 Mar", what: "Streaming subscription", who: "Sam", state: "done" },
  ],
};

function render(list, items) {
  const el = document.getElementById(list);
  el.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Nothing here.";
    el.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");

    const when = document.createElement("span");
    when.className = "when";
    when.textContent = item.when;

    const what = document.createElement("span");
    what.className = "what";
    what.textContent = item.what;

    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.who;

    const state = document.createElement("span");
    state.className = `state ${item.state || ""}`;
    state.textContent = item.state || "";

    li.append(when, what, who, state);
    el.appendChild(li);
  }
}

render("due", FIXTURE.due);
render("items", FIXTURE.items);
