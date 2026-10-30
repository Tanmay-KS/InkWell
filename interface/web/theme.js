const themeToggle = document.getElementById("theme-toggle");
const themeColorMeta = document.querySelector('meta[name="theme-color"]');

function applyTheme(theme) {
  const isLight = theme === "light";
  document.body.classList.toggle("theme-light", isLight);
  if (themeToggle) {
    const nextTheme = isLight ? "dark" : "light";
    themeToggle.classList.toggle("is-light", isLight);
    themeToggle.setAttribute("aria-pressed", String(isLight));
    themeToggle.setAttribute("aria-label", `Switch to ${nextTheme} mode`);
    themeToggle.title = `Switch to ${nextTheme} mode`;
  }
  if (themeColorMeta) themeColorMeta.content = isLight ? "#f6f5f0" : "#171a1c";
}

applyTheme(localStorage.getItem("inkwell-theme") || "dark");

themeToggle?.addEventListener("click", () => {
  const nextTheme = document.body.classList.contains("theme-light") ? "dark" : "light";
  localStorage.setItem("inkwell-theme", nextTheme);
  applyTheme(nextTheme);
});

window.addEventListener("storage", (event) => {
  if (event.key === "inkwell-theme") applyTheme(event.newValue || "dark");
});
