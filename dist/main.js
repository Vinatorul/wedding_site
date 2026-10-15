const content = window.weddingContent ?? {};

document.querySelectorAll("[data-content]").forEach((element) => {
  const value = content[element.dataset.content];
  if (typeof value === "string") element.textContent = value;
});

function flipVenueCard(card, showMap) {
  const front = card.querySelector(".venue-front");
  const back = card.querySelector(".venue-back");
  const activeFace = showMap ? back : front;
  const inactiveFace = showMap ? front : back;
  card.classList.toggle("is-flipped", showMap);
  activeFace.inert = false;
  activeFace.setAttribute("aria-hidden", "false");
  card
    .querySelector('[data-venue-flip="map"]')
    .setAttribute("aria-expanded", String(showMap));
  activeFace.querySelector("[data-venue-flip]").focus({ preventScroll: true });
  inactiveFace.inert = true;
  inactiveFace.setAttribute("aria-hidden", "true");
}

async function turnVenueCard(card, showMap) {
  if (card.dataset.turning) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    flipVenueCard(card, showMap);
    return;
  }
  card.dataset.turning = "true";
  const inner = card.querySelector(".venue-card-inner");
  try {
    await inner.animate(
      { transform: ["rotateY(0)", "rotateY(90deg)"] },
      { duration: 180, easing: "ease-in" },
    ).finished;
    flipVenueCard(card, showMap);
    await inner.animate(
      { transform: ["rotateY(-90deg)", "rotateY(0)"] },
      { duration: 180, easing: "ease-out" },
    ).finished;
  } finally {
    delete card.dataset.turning;
  }
}

document.querySelectorAll(".venue-card").forEach((card) => {
  card.querySelectorAll("[data-venue-flip]").forEach((button) => {
    button.addEventListener("click", () => {
      turnVenueCard(card, button.dataset.venueFlip === "map");
    });
  });
});
