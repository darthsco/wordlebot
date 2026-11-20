// Passkey (WebAuthn) ceremonies for the admin panel.
//
// Loaded on every page; the CSP allows no inline script. A button opts in with
// data-passkey="login" | "enroll" | "add". The server sends options as JSON
// (binary fields base64url-encoded), and each response goes back the same way.
(() => {
  "use strict";

  const fromB64 = (text) => {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0)).buffer;
  };
  const toB64 = (buffer) => {
    let raw = "";
    for (const byte of new Uint8Array(buffer)) raw += String.fromCharCode(byte);
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const withIds = (list) => (list || []).map((c) => ({ ...c, id: fromB64(c.id) }));

  // Browsers from 2024-25 parse and serialise these themselves; older ones get the
  // same conversion by hand.
  const creationOptions = (o) =>
    PublicKeyCredential.parseCreationOptionsFromJSON
      ? PublicKeyCredential.parseCreationOptionsFromJSON(o)
      : {
          ...o,
          challenge: fromB64(o.challenge),
          user: { ...o.user, id: fromB64(o.user.id) },
          excludeCredentials: withIds(o.excludeCredentials),
        };
  const requestOptions = (o) =>
    PublicKeyCredential.parseRequestOptionsFromJSON
      ? PublicKeyCredential.parseRequestOptionsFromJSON(o)
      : { ...o, challenge: fromB64(o.challenge), allowCredentials: withIds(o.allowCredentials) };

  const credentialJSON = (cred) => {
    try {
      if (cred.toJSON) return cred.toJSON();
    } catch (_) {
      // Some password managers return objects whose toJSON throws.
    }
    const r = cred.response;
    const response = { clientDataJSON: toB64(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = toB64(r.attestationObject);
      response.transports = r.getTransports ? r.getTransports() : [];
    } else {
      response.authenticatorData = toB64(r.authenticatorData);
      response.signature = toB64(r.signature);
      if (r.userHandle) response.userHandle = toB64(r.userHandle);
    }
    return {
      id: cred.id,
      rawId: toB64(cred.rawId),
      type: cred.type,
      response,
      clientExtensionResults: cred.getClientExtensionResults(),
      authenticatorAttachment: cred.authenticatorAttachment,
    };
  };

  const post = async (url, body, csrf) => {
    const headers = { "Content-Type": "application/json" };
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      throw new Error(response.ok ? "Unexpected response." : await response.text().catch(() => ""));
    }
    if (!response.ok || data.error) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  };

  const create = async (options) =>
    credentialJSON(await navigator.credentials.create({ publicKey: creationOptions(options) }));
  const get = async (options) =>
    credentialJSON(await navigator.credentials.get({ publicKey: requestOptions(options) }));

  const label = (button) => {
    const input = button.dataset.label && document.getElementById(button.dataset.label);
    return input ? input.value : "";
  };

  const flows = {
    async login() {
      const { get: options } = await post("/login/options");
      return post("/login", { credential: await get(options) });
    },
    async enroll(button) {
      const url = button.dataset.url;
      const { create: options } = await post(`${url}/options`);
      return post(url, { credential: await create(options), label: label(button) });
    },
    async add(button) {
      const csrf = button.dataset.csrf;
      let step = await post("/account/passkeys/options", {}, csrf);
      if (step.reauth) {
        // Confirm with an existing passkey before a new one can be added.
        step = await post("/account/passkeys/reauth", { credential: await get(step.reauth) }, csrf);
      }
      return post("/account/passkeys", { credential: await create(step.create), label: label(button) }, csrf);
    },
  };

  const explain = (error) => {
    if (error && error.name === "NotAllowedError") return "Cancelled, or no passkey for this site was chosen.";
    if (error && error.name === "InvalidStateError") return "This device already has a passkey for your account.";
    if (error && error.name === "SecurityError") return "Passkeys don't work on this address; use the panel's https:// domain.";
    return (error && error.message) || "Something went wrong.";
  };

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-passkey]");
    if (!button || button.disabled) return;
    const flow = flows[button.dataset.passkey];
    if (!flow) return;
    event.preventDefault();
    const card = button.closest("section, form, .card") || document.body;
    const errorBox = card.querySelector("[data-passkey-error]");
    if (errorBox) errorBox.textContent = "";
    if (!window.PublicKeyCredential) {
      if (errorBox) errorBox.textContent = "This browser doesn't support passkeys.";
      return;
    }
    button.disabled = true;
    try {
      const result = await flow(button);
      window.location.assign(result.redirect || "/");
    } catch (error) {
      if (errorBox) errorBox.textContent = explain(error);
      button.disabled = false;
    }
  });
})();
