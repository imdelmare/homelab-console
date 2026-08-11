import {
  ApiError,
  completeChallenge,
  fetchAuthConfig,
  login,
  pollChallenge,
  verifyOtp,
} from "../src/lib/api";
import { formatCountdown, shortId } from "../src/lib/format";
import type { AuthCompleteResponse, AuthLoginMode, LoginChallenge } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

type Stage = "form" | "waiting" | "otp";

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("Authentication configuration timed out.")), timeoutMs);
    promise.then(
      (value) => { window.clearTimeout(timer); resolve(value); },
      (error) => { window.clearTimeout(timer); reject(error); },
    );
  });
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Invalid credentials.";
    if (error.status === 429) return "Too many attempts. Try again later.";
    return error.message;
  }
  return "Unable to reach the server.";
}

export function mountLogin(target: HTMLElement, onAuthenticated: (result: AuthCompleteResponse) => void): () => void {
  let stage: Stage = "form";
  let loginMode: AuthLoginMode | null = null;
  let challenge: LoginChallenge | null = null;
  let busy = false;
  let errorMessage = "";
  let pollTimer: number | null = null;
  let countdownTimer: number | null = null;
  let active = true;

  function clearTimers(): void {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    if (countdownTimer !== null) window.clearInterval(countdownTimer);
    pollTimer = null;
    countdownTimer = null;
  }

  function field(label: string, input: HTMLInputElement): HTMLElement {
    return element("label", { className: "field" }, element("span", {}, label), input);
  }

  function cancelChallenge(): void {
    clearTimers();
    challenge = null;
    stage = "form";
    errorMessage = "";
    render();
  }

  async function finishChallenge(challengeId: string): Promise<void> {
    try {
      const result = await completeChallenge(challengeId);
      if (active) onAuthenticated(result);
    } catch (error) {
      if (!active) return;
      errorMessage = describeError(error);
      challenge = null;
      stage = "form";
      render();
    }
  }

  function startChallengeTimers(): void {
    clearTimers();
    countdownTimer = window.setInterval(render, 1000);
    pollTimer = window.setInterval(async () => {
      if (!challenge) return;
      const challengeId = challenge.challenge_id;
      try {
        const result = await pollChallenge(challengeId);
        if (!active || !challenge || challenge.challenge_id !== challengeId) return;
        if (result.status === "approved") {
          clearTimers();
          await finishChallenge(challengeId);
        } else if (result.status === "rejected") {
          clearTimers();
          errorMessage = "The sign-in request was rejected.";
          challenge = null;
          stage = "form";
          render();
        } else if (result.status === "expired" || result.status === "consumed") {
          clearTimers();
          errorMessage = "The sign-in request expired. Try again.";
          challenge = null;
          stage = "form";
          render();
        }
      } catch (error) {
        if (!active) return;
        clearTimers();
        errorMessage = describeError(error);
        challenge = null;
        stage = "form";
        render();
      }
    }, 2000);
  }

  function renderForm(): HTMLElement {
    if (loginMode === null) {
      if (errorMessage) {
        const retry = button("Try again", "button button--primary");
        retry.addEventListener("click", () => void loadConfig());
        return element("div", { className: "login-form" }, element("p", { className: "error-banner", role: "alert" }, errorMessage), element("div", { className: "dialog-actions" }, retry));
      }
      return element("div", { className: "loading-state" }, element("span", { className: "hourglass" }, "⌛"), "Loading authentication…");
    }
    const username = element("input", { id: "html-login-username", className: "text-input", autocomplete: "username", required: true });
    const password = element("input", { id: "html-login-password", className: "text-input", type: "password", autocomplete: "current-password", required: true });
    const submit = element("button", { className: "button button--primary", type: "submit" }, busy ? "Verifying…" : "Sign in");
    const form = element(
      "form",
      { className: "login-form" },
      field("Username", username),
      loginMode === "password" ? field("Password", password) : null,
      errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null,
      element("div", { className: "dialog-actions" }, submit),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      busy = true;
      errorMessage = "";
      submit.disabled = true;
      submit.textContent = "Verifying…";
      try {
        challenge = await login(username.value, loginMode === "password" ? password.value : undefined);
        if (!active) return;
        stage = "waiting";
        render();
        startChallengeTimers();
      } catch (error) {
        if (!active) return;
        errorMessage = describeError(error);
        busy = false;
        render();
      }
    });
    queueMicrotask(() => username.focus());
    return form;
  }

  function renderWaiting(): HTMLElement {
    if (!challenge) return renderForm();
    const useCode = button("Use a code");
    const cancel = button("Cancel");
    useCode.addEventListener("click", () => {
      stage = "otp";
      render();
    });
    cancel.addEventListener("click", cancelChallenge);
    return element(
      "div",
      { className: "login-waiting" },
      element("div", { className: "telegram-glyph", "aria-hidden": "true" }, "➤"),
      element("h2", {}, "Check Telegram"),
      element("p", {}, "A sign-in request is waiting for your approval."),
      element("div", { className: "challenge-readout" },
        element("span", {}, "REQUEST"), element("strong", {}, shortId(challenge.challenge_id)),
        element("span", {}, "EXPIRES"), element("strong", {}, formatCountdown(challenge.expires_at)),
      ),
      element("div", { className: "dialog-actions" }, useCode, cancel),
    );
  }

  function renderOtp(): HTMLElement {
    if (!challenge) return renderForm();
    const otp = element("input", { id: "html-login-otp", className: "text-input otp-input", autocomplete: "one-time-code", inputmode: "numeric", required: true });
    const submit = element("button", { className: "button button--primary", type: "submit" }, "Verify");
    const back = button("Back");
    const cancel = button("Cancel");
    const form = element(
      "form",
      { className: "login-form" },
      element("h2", {}, "One-time code"),
      element("p", {}, `Challenge expires in ${formatCountdown(challenge.expires_at)}.`),
      field("Authenticator code", otp),
      errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null,
      element("div", { className: "dialog-actions" }, submit, back, cancel),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!challenge) return;
      busy = true;
      submit.disabled = true;
      try {
        const result = await verifyOtp(challenge.challenge_id, otp.value);
        if (active) onAuthenticated(result);
      } catch (error) {
        if (!active) return;
        errorMessage = describeError(error);
        busy = false;
        render();
      }
    });
    back.addEventListener("click", () => {
      stage = "waiting";
      render();
    });
    cancel.addEventListener("click", cancelChallenge);
    queueMicrotask(() => otp.focus());
    return form;
  }

  function render(): void {
    if (!active) return;
    const content = stage === "form" ? renderForm() : stage === "waiting" ? renderWaiting() : renderOtp();
    replaceChildren(
      target,
      element(
        "main",
        { className: "login-screen" },
        element("section", { className: "window login-window", "aria-labelledby": "login-title" },
          element("header", { className: "title-bar" }, element("h1", { id: "login-title" }, "Secure sign in"), element("span", {}, "_  □  ×")),
          element("div", { className: "window-body" }, content),
        ),
      ),
    );
  }

  async function loadConfig(): Promise<void> {
    errorMessage = "";
    render();
    try {
      const config = await withTimeout(fetchAuthConfig(), 8_000);
      if (!active) return;
      loginMode = config.login_mode;
      errorMessage = "";
    } catch {
      if (!active) return;
      errorMessage = "Unable to load the authentication method.";
    }
    render();
  }

  void loadConfig();
  return () => {
    active = false;
    clearTimers();
  };
}
