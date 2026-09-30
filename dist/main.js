const content = window.weddingContent ?? {};

document.querySelectorAll("[data-content]").forEach((element) => {
  const value = content[element.dataset.content];
  if (typeof value === "string") element.textContent = value;
});

function updateAttendance() {
  const attending = document.querySelector('[name="attendance"]:checked');
  const drink = document.getElementById("guest-drink");
  drink.disabled = attending?.value === "no";
  document.getElementById("form-status").textContent = "";
}

document.getElementById("rsvp-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = document.getElementById("guest-name");
  if (!name.value.trim()) {
    name.setCustomValidity("Напиши своё имя.");
    name.reportValidity();
    return;
  }
  document.getElementById("form-status").textContent =
    "Всё заполнено! Это макет: ответ никуда не отправлен.";
});

document.getElementById("guest-name").addEventListener("input", (event) => {
  event.target.setCustomValidity("");
});
document.getElementById("rsvp-form").addEventListener("input", () => {
  document.getElementById("form-status").textContent = "";
});
document.querySelectorAll('[name="attendance"]').forEach((radio) => {
  radio.addEventListener("change", updateAttendance);
});
updateAttendance();

function setPalette(theme) {
  document.documentElement.dataset.theme = theme;
  const url = new URL(window.location.href);
  url.searchParams.set("theme", theme);
  window.history.replaceState(null, "", url);
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.themeChoice === theme);
  });
  const paper = getComputedStyle(document.documentElement)
    .getPropertyValue("--paper")
    .trim();
  document.querySelector('meta[name="theme-color"]').content = paper;
}

document.querySelectorAll("[data-theme-choice]").forEach((button) => {
  button.addEventListener("click", () =>
    setPalette(button.dataset.themeChoice),
  );
});
setPalette(document.documentElement.dataset.theme);
