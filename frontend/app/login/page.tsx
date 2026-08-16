"use client";
// Branded sign-in that replaces the browser's native Basic-Auth dialog. Posts credentials to the
// server route (which validates against env and sets the HttpOnly session cookie), then navigates to
// the originally-requested page. The password is never held in the client bundle or any NEXT_PUBLIC.
import React, { useState } from "react";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        setError("Invalid username or password.");
        setBusy(false);
        return;
      }
      // Full navigation so the new cookie applies and the dashboard loads fresh. Only allow internal paths.
      const next = new URLSearchParams(window.location.search).get("next") || "/";
      const safe = next.startsWith("/") && !next.startsWith("//") ? next : "/";
      window.location.assign(safe);
    } catch {
      setError("Sign-in failed. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={onSubmit}>
        <div className="login-brand">
          <span className="logo">
            <svg viewBox="0 0 34 34" aria-hidden="true">
              <defs><linearGradient id="glg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#35D0BA" /><stop offset="1" stopColor="#0E8C7E" /></linearGradient></defs>
              <path d="M17 2 30 9.5v15L17 32 4 24.5v-15Z" fill="none" stroke="url(#glg)" strokeWidth="1.6" />
              <path d="M17 9 24 13v8l-7 4-7-4v-8Z" fill="none" stroke="#35D0BA" strokeWidth="1.3" opacity=".7" />
              <circle cx="17" cy="17" r="2.4" fill="#35D0BA" />
            </svg>
          </span>
          <div><h1>GIGBAY&nbsp;AI</h1><p>AI Trading Command Center</p></div>
        </div>

        <h2>Sign in</h2>
        <p className="lead">Authorized access only — read-only command center.</p>

        {error ? (
          <div className="login-err" role="alert">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16v.5" /></svg>
            {error}
          </div>
        ) : null}

        <div className="login-field">
          <label htmlFor="gb-user">Username</label>
          <input id="gb-user" name="username" autoComplete="username" autoFocus
            value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
        </div>
        <div className="login-field">
          <label htmlFor="gb-pass">Password</label>
          <input id="gb-pass" name="password" type="password" autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••••" />
        </div>

        <button className="login-btn" type="submit" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <div className="login-foot"><span className="dot" />Encrypted connection · GIGBAY AI</div>
      </form>
    </div>
  );
}
