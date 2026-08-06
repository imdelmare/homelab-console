import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Button, TextInput as Input, Window, WindowContent, WindowHeader } from "react95";
import { ApiError, completeChallenge, fetchAuthConfig, login, pollChallenge, verifyOtp } from "../lib/api";
import { formatCountdown, shortId } from "../lib/format";
import type { AuthCompleteResponse, AuthLoginMode, LoginChallenge } from "../lib/types";
import { LoadingIndicator } from "./LoadingIndicator";

type Stage = "form" | "waiting" | "otp";

type LoginWindowProps = {
  onAuthenticated: (result: AuthCompleteResponse) => void;
};

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "Invalid credentials.";
    }
    if (error.status === 429) {
      return "Too many attempts. Try again later.";
    }
    return error.message;
  }
  return "Unable to reach the server.";
}

export function LoginWindow({ onAuthenticated }: LoginWindowProps) {
  const [stage, setStage] = useState<Stage>("form");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [otp, setOtp] = useState("");
  const [challenge, setChallenge] = useState<LoginChallenge | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [loginMode, setLoginMode] = useState<AuthLoginMode | null>(null);
  const [authConfigError, setAuthConfigError] = useState(false);
  const [authConfigAttempt, setAuthConfigAttempt] = useState(0);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isWaitingOnChallenge = stage === "waiting" || stage === "otp";

  useEffect(() => {
    let active = true;
    fetchAuthConfig()
      .then((config) => {
        if (active) {
          setLoginMode(config.login_mode);
          setAuthConfigError(false);
        }
      })
      .catch(() => {
        if (active) setAuthConfigError(true);
      });
    return () => {
      active = false;
    };
  }, [authConfigAttempt]);

  // Tick every second while a challenge is outstanding, to drive the countdown.
  useEffect(() => {
    if (!isWaitingOnChallenge) {
      return;
    }
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [isWaitingOnChallenge]);

  // Poll challenge status every 2s while waiting (form or OTP stage), stop otherwise.
  useEffect(() => {
    if (!isWaitingOnChallenge || !challenge) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    const challengeId = challenge.challenge_id;

    const timer = setInterval(async () => {
      try {
        const poll = await pollChallenge(challengeId);
        if (poll.status === "approved") {
          clearInterval(timer);
          pollTimerRef.current = null;
          const result = await completeChallenge(challengeId);
          onAuthenticated(result);
        } else if (poll.status === "rejected") {
          clearInterval(timer);
          pollTimerRef.current = null;
          setErrorMessage("The sign-in request was rejected.");
          setChallenge(null);
          setStage("form");
        } else if (poll.status === "expired" || poll.status === "consumed") {
          clearInterval(timer);
          pollTimerRef.current = null;
          setErrorMessage("The sign-in request expired. Try again.");
          setChallenge(null);
          setStage("form");
        }
      } catch (error) {
        clearInterval(timer);
        pollTimerRef.current = null;
        setErrorMessage(describeError(error));
        setChallenge(null);
        setStage("form");
      }
    }, 2000);

    pollTimerRef.current = timer;
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isWaitingOnChallenge, challenge?.challenge_id]);

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setErrorMessage(null);
    try {
      const result = await login(
        username,
        loginMode === "password" ? password : undefined,
      );
      setChallenge(result);
      setStage("waiting");
    } catch (error) {
      setErrorMessage(describeError(error));
    } finally {
      setBusy(false);
    }
  }

  async function submitOtp(event: FormEvent) {
    event.preventDefault();
    if (!challenge) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    try {
      const result = await verifyOtp(challenge.challenge_id, otp);
      onAuthenticated(result);
    } catch (error) {
      setErrorMessage(describeError(error));
    } finally {
      setBusy(false);
    }
  }

  function cancelChallenge() {
    setChallenge(null);
    setOtp("");
    setErrorMessage(null);
    setStage("form");
  }

  const countdown = challenge ? formatCountdown(challenge.expires_at, now) : "0:00";

  return (
    <div className="login-screen">
      <Window className="window login-window react95-window">
        <WindowHeader active className="title-bar"><span className="window-title">Sign in</span></WindowHeader>
        <WindowContent className="window-body login-window-body">
          {stage === "form" && loginMode === null && !authConfigError && (
            <LoadingIndicator label="Loading authentication…" />
          )}
          {stage === "form" && loginMode === null && authConfigError && (
            <div className="login-waiting">
              <p className="login-error" role="alert">Unable to load the authentication method.</p>
              <div className="dialog-actions">
                <Button type="button" onClick={() => {
                  setAuthConfigError(false);
                  setAuthConfigAttempt((attempt) => attempt + 1);
                }}>
                  Try again
                </Button>
              </div>
            </div>
          )}
          {stage === "form" && loginMode !== null && (
            <form onSubmit={submitLogin}>
              <div className="field-row-stacked">
                <label htmlFor="login-username">Username</label>
                <Input
                  id="login-username"
                  autoFocus
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  disabled={busy}
                />
              </div>
              {loginMode === "password" && (
                <div className="field-row-stacked">
                  <label htmlFor="login-password">Password</label>
                  <div className="password-control">
                    <Input
                      id="login-password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      disabled={busy}
                    />
                    <Button type="button" onClick={() => setShowPassword((shown) => !shown)} disabled={busy} aria-pressed={showPassword}>
                      {showPassword ? "Hide" : "Show"}
                    </Button>
                  </div>
                </div>
              )}
              {errorMessage && <p className="login-error" role="alert">{errorMessage}</p>}
              <div className="dialog-actions">
                <Button
                  type="submit"
                  disabled={busy || !username || (loginMode === "password" && !password)}
                >
                  {busy ? "Verifying…" : "Sign in"}
                </Button>
              </div>
            </form>
          )}

          {stage === "waiting" && challenge && (
            <div className="login-waiting">
              <p>Waiting for approval on Telegram…</p>
              <p className="system-line">
                Request <strong>{shortId(challenge.challenge_id)}</strong>
              </p>
              <p className="system-line">
                Expires in <strong>{countdown}</strong>
              </p>
              {errorMessage && <p className="login-error">{errorMessage}</p>}
              <div className="dialog-actions">
                <Button type="button" onClick={() => setStage("otp")}>
                  Use a code
                </Button>
                <Button type="button" onClick={cancelChallenge}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {stage === "otp" && challenge && (
            <form onSubmit={submitOtp} className="login-otp">
              <p>Enter the authenticator one-time code.</p>
              <p className="system-line">
                Expires in <strong>{countdown}</strong>
              </p>
              <div className="field-row-stacked">
                <label htmlFor="login-otp">Code</label>
                <Input
                  id="login-otp"
                  autoFocus
                  autoComplete="one-time-code"
                  value={otp}
                  onChange={(event) => setOtp(event.target.value)}
                  disabled={busy}
                />
              </div>
              {errorMessage && <p className="login-error">{errorMessage}</p>}
              <div className="dialog-actions">
                <Button type="submit" disabled={busy || !otp}>
                  Verify
                </Button>
                <Button type="button" onClick={() => setStage("waiting")}>
                  Back
                </Button>
                <Button type="button" onClick={cancelChallenge}>
                  Cancel
                </Button>
              </div>
            </form>
          )}
        </WindowContent>
      </Window>
    </div>
  );
}
