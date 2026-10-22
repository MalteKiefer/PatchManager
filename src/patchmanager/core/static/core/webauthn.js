// WebAuthn ceremonies. Buttons declare data-webauthn="register|login" plus
// data-options-url and data-verify-url. The CSRF token comes from the page.

// Translated texts from <div id="js-text" data-...> in the base layout.
const text = (key, fallback = "") => document.getElementById("js-text")?.dataset[key] || fallback;

const b64 = {
  decode(value) {
    const s = value.replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(s + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer;
  },
  encode(buffer) {
    return btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

function csrf() {
  return document.querySelector("meta[name=csrf-token]").content;
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-CSRFToken": csrf(), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || text("failed", "Request failed"));
  return data;
}

function serialise(credential) {
  const r = credential.response;
  const out = {
    id: credential.id,
    rawId: b64.encode(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: { clientDataJSON: b64.encode(r.clientDataJSON) },
  };
  if (r.attestationObject) {
    out.response.attestationObject = b64.encode(r.attestationObject);
    out.response.transports = r.getTransports ? r.getTransports() : [];
  } else {
    out.response.authenticatorData = b64.encode(r.authenticatorData);
    out.response.signature = b64.encode(r.signature);
    out.response.userHandle = r.userHandle ? b64.encode(r.userHandle) : null;
  }
  return out;
}

async function run(button) {
  const status = document.getElementById(button.dataset.status || "webauthn-status");
  const say = (text) => { if (status) status.textContent = text; };
  try {
    say("");
    const options = JSON.parse(await post(button.dataset.optionsUrl));
    options.challenge = b64.decode(options.challenge);
    for (const list of [options.excludeCredentials, options.allowCredentials]) {
      for (const c of list || []) c.id = b64.decode(c.id);
    }
    let credential;
    if (button.dataset.webauthn === "register") {
      options.user.id = b64.decode(options.user.id);
      credential = await navigator.credentials.create({ publicKey: options });
    } else {
      credential = await navigator.credentials.get({ publicKey: options });
    }
    const nameInput = document.getElementById(button.dataset.nameInput || "");
    const url = new URL(button.dataset.verifyUrl, location.href);
    if (nameInput) url.searchParams.set("name", nameInput.value);
    const result = await post(url, serialise(credential));
    location.assign(result.redirect || "/");
  } catch (error) {
    say(error.name === "NotAllowedError" ? text("keyCancelled", "The request was cancelled or timed out.") : error.message);
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-webauthn]");
  if (!button) return;
  event.preventDefault();
  if (!window.PublicKeyCredential) {
    document.getElementById(button.dataset.status || "webauthn-status").textContent = text("keyUnsupported", "This browser does not support security keys.");
    return;
  }
  run(button);
});
