const form = document.querySelector("#lookup-form");
const urlInput = document.querySelector("#business-url");
const clearButton = document.querySelector("#clear-button");
const submitButton = document.querySelector("#submit-button");
const formMessage = document.querySelector("#form-message");
const resultContent = document.querySelector("#result-content");
const resultSuccess = document.querySelector("#result-success");
const placeChoices = document.querySelector("#place-choices");
const reviewLink = document.querySelector("#review-link");
const openButton = document.querySelector("#open-button");
const copyButton = document.querySelector("#copy-button");
const toast = document.querySelector("#toast");
let toastTimer;

function setMessage(message, type = "error") {
  formMessage.textContent = message;
  formMessage.classList.toggle("info", type === "info");
  formMessage.hidden = !message;
}

function setBusy(isBusy) {
  submitButton.disabled = isBusy;
  submitButton.querySelector(".button-label").textContent = isBusy ? "جارٍ البحث عن النشاط..." : "استخراج رابط التقييم";
  submitButton.querySelector(".button-arrow").innerHTML = isBusy ? '<span class="spinner" aria-hidden="true"></span>' : "←";
}

function parseGoogleLink(rawValue) {
  let parsed;
  try {
    parsed = new URL(rawValue.trim());
  } catch {
    throw new Error("أدخل رابطاً كاملاً يبدأ بـ https:// ثم حاول مرة أخرى.");
  }

  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  const allowedHost = host === "google.com"
    || host.endsWith(".google.com")
    || host === "maps.app.goo.gl"
    || host === "g.page"
    || host.endsWith(".business.site");
  if (parsed.protocol !== "https:" || !allowedHost) {
    throw new Error("الرابط غير معروف كرابط Google. استخدم رابط نشاطك من Google Maps أو ملفك التجاري.");
  }

  if (host === "g.page") {
    const reviewId = parsed.pathname.match(/^\/r\/([^/]+)(?:\/review)?\/?$/i)?.[1];
    if (reviewId) return { directUrl: `https://g.page/r/${encodeURIComponent(reviewId)}/review` };
  }

  const idParameters = ["query_place_id", "place_id", "destination_place_id", "origin_place_id"];
  for (const parameter of idParameters) {
    const value = parsed.searchParams.get(parameter);
    if (value) return { placeId: value };
  }

  const query = parsed.searchParams.get("q") || parsed.searchParams.get("query") || "";
  const queryMatch = query.match(/place_id\s*:\s*([\w-]+)/i);
  if (queryMatch) return { placeId: queryMatch[1] };

  const pathAndData = `${parsed.pathname} ${parsed.search} ${parsed.hash}`;
  const dataMatch = pathAndData.match(/!1s(ChIJ[\w-]+)/);
  if (dataMatch) return { placeId: dataMatch[1] };

  return {};
}

async function resolveGoogleShortLink(url) {
  const response = await fetch("/api/resolve-share-link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "تعذر قراءة رابط Google المختصر.");
  return result;
}

function makeReviewUrl(placeId) {
  return `https://search.google.com/local/writereview?placeid=${encodeURIComponent(placeId)}`;
}

function showReviewUrl(url) {
  reviewLink.href = url;
  reviewLink.textContent = url;
  openButton.href = url;
  resultContent.hidden = true;
  placeChoices.hidden = true;
  resultSuccess.hidden = false;
  setMessage("تم تجهيز رابط التقييم. افتحه للتأكد من ظهور النشاط الصحيح قبل استخدامه.", "info");
}

function renderPlaceChoices(places) {
  placeChoices.replaceChildren();
  for (const place of places) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "place-choice";
    const details = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = place.name;
    const address = document.createElement("small");
    address.textContent = place.address || "العنوان غير متاح";
    const arrow = document.createElement("span");
    arrow.className = "choice-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "←";
    details.append(name, address);
    button.append(details, arrow);
    button.addEventListener("click", () => showReviewUrl(makeReviewUrl(place.place_id)));
    placeChoices.append(button);
  }
  resultContent.hidden = true;
  resultSuccess.hidden = true;
  placeChoices.hidden = false;
  setMessage("اختر الفرع المطابق لملفك التجاري.", "info");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage("");
  resultSuccess.hidden = true;
  placeChoices.hidden = true;

  try {
    const value = urlInput.value.trim();
    if (!value) throw new Error("ألصق رابط ملف نشاطك التجاري أولاً.");

    let resolvedUrl = value;
    let shareResult;
    const inputHost = new URL(value).hostname.toLowerCase();
    if (inputHost === "share.google" || inputHost === "maps.app.goo.gl") {
      setBusy(true);
      shareResult = await resolveGoogleShortLink(value);
      resolvedUrl = shareResult.resolved_url;
    }

    const parsed = parseGoogleLink(resolvedUrl);
    if (shareResult?.review_url) {
      showReviewUrl(shareResult.review_url);
      return;
    }
    if (shareResult?.places?.length) {
      renderPlaceChoices(shareResult.places);
      return;
    }
    if (parsed.directUrl) {
      showReviewUrl(parsed.directUrl);
      return;
    }
    if (parsed.placeId) {
      showReviewUrl(makeReviewUrl(parsed.placeId));
      return;
    }
    throw new Error("لم يتضمن الرابط معرّف المكان المطلوب. استخدم رابط Google Maps يحتوي Place ID أو رابط g.page بصيغة /r/{ID}.");
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "تعذر تجهيز الرابط. تحقق من البيانات وحاول مرة أخرى.");
    resultContent.hidden = false;
    placeChoices.hidden = true;
  } finally {
    setBusy(false);
  }
});

urlInput.addEventListener("input", () => {
  clearButton.hidden = !urlInput.value;
  setMessage("");
});

clearButton.addEventListener("click", () => {
  urlInput.value = "";
  clearButton.hidden = true;
  resultContent.hidden = false;
  resultSuccess.hidden = true;
  placeChoices.hidden = true;
  setMessage("");
  urlInput.focus();
});

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(reviewLink.href);
    toast.textContent = "تم نسخ رابط التقييم";
  } catch {
    const temporaryInput = document.createElement("textarea");
    temporaryInput.value = reviewLink.href;
    temporaryInput.style.position = "fixed";
    temporaryInput.style.opacity = "0";
    document.body.append(temporaryInput);
    temporaryInput.select();
    const copied = document.execCommand("copy");
    temporaryInput.remove();
    toast.textContent = copied ? "تم نسخ رابط التقييم" : "تعذر النسخ تلقائياً؛ حدد الرابط وانسخه يدوياً.";
  }
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 2400);
});