const initialPalette = new URLSearchParams(window.location.search).get("theme");
document.documentElement.dataset.theme = ["burgundy", "autumn"].includes(
  initialPalette,
)
  ? initialPalette
  : "burgundy";
