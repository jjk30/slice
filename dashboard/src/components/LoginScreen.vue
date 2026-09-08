<script setup>
import { computed, ref } from 'vue'
import { apiBase } from '../api.js'
import { lastLogin, forgetLogin } from '../auth.js'
import githubMark from '../assets/github-mark.png'

// Phase 21: GitHub sign-in for the dashboard. The button is a full-page redirect to the
// gateway's /auth/github/login, which sends the browser to GitHub and back and sets the
// session cookie. If the browser came back from a cancelled or failed sign-in, the URL
// carries ?login=denied or ?login=failed and we show one plain line about it.
// Phase 22a: `signedOut` is set by App.vue right after a Log out, so this screen shows a
// one-line "Signed out" note instead of looking like a fresh, never-signed-in visit.
// Returning mode: when a previous sign-in left its GitHub username in localStorage (see
// rememberLogin in auth.js, written only from the gateway's own session), the card says
// "Welcome back." and the button reads "Log in as {name}". It is the same GitHub
// sign-in either way; the name is only wording. "Use a different GitHub account" forgets
// the name and drops the card back to the plain mode without a reload.
const props = defineProps({
  signedOut: { type: Boolean, default: false },
})

const remembered = ref(lastLogin())

function useDifferentAccount() {
  forgetLogin()
  remembered.value = null
}

const loginError = computed(() => {
  const reason = new URLSearchParams(window.location.search).get('login')
  if (reason === 'denied') return 'Sign-in was cancelled on GitHub.'
  if (reason === 'failed') return 'Sign-in did not complete. Try again.'
  return ''
})

function signIn() {
  window.location.href = apiBase() + '/auth/github/login'
}
</script>

<template>
  <div class="login">
    <div class="login-card card">
      <div class="brand">
        <img class="brand-logo" src="/favicon.png" alt="" width="28" height="28" />
        <h1 class="brand-name">slice</h1>
      </div>
      <p class="lede">{{ remembered ? 'Welcome back.' : 'Sign in to your dashboard.' }}</p>
      <p v-if="signedOut && !loginError && !remembered" class="signed-out" role="status">Signed out</p>
      <p v-if="loginError" class="login-error" role="alert">{{ loginError }}</p>
      <button type="button" class="submit" @click="signIn">
        <img class="gh-mark" :src="githubMark" alt="" />{{ remembered ? `Log in as ${remembered}` : 'Sign in with GitHub' }}
      </button>
      <button v-if="remembered" type="button" class="switch-account" @click="useDifferentAccount">
        Use a different GitHub account
      </button>
      <p class="new-here">
        New here? Install the CLI first, about three minutes.
        <a href="https://sliceapp.dev/how-to#install">Set up slice</a>
      </p>
      <p class="hint">
        The terminal uses <code class="mono">slice login</code> instead, which hands out a
        slice key for the CLI. This dashboard signs you in through GitHub.
      </p>
    </div>
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: var(--s3);
  background: var(--paper);
}

.login-card {
  width: 100%;
  max-width: 380px;
  padding: var(--s4);
  display: flex;
  flex-direction: column;
  gap: var(--s2);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-name {
  font-family: var(--display);
  font-size: 24px;
  font-weight: 700;
  margin: 0;
  color: var(--ink);
}

.lede {
  margin: 0;
  color: var(--muted);
  font-size: 14px;
}

.login-error {
  margin: 0;
  font-size: 13px;
  color: var(--cherry, #b3261e);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.label {
  font-size: 12px;
  color: var(--muted);
}

.key-row {
  display: flex;
  gap: 8px;
}

.input {
  flex: 1;
  padding: 10px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--card);
  color: var(--ink);
  font-size: 13px;
}

.input:focus {
  outline: 2px solid var(--teal);
  outline-offset: 1px;
  border-color: transparent;
}

.reveal {
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
}

.signed-out {
  margin: 0;
  font-size: 13px;
  color: var(--muted);
}

.submit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 11px 14px;
  border: 0;
  border-radius: 10px;
  background: var(--teal);
  color: #fff;
  font-weight: 500;
  font-size: 14px;
  cursor: pointer;
}

/* The GitHub mark, used as-is: fixed height, width follows its aspect ratio (no stretch). */
.gh-mark {
  height: 18px;
  width: auto;
  display: block;
}

.submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* A link in the muted body style; a button underneath so it is keyboard-reachable. */
.switch-account {
  align-self: flex-start;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  font-family: var(--body);
  font-size: 14px;
  line-height: 1.5;
  color: var(--muted);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}

.switch-account:hover {
  color: var(--ink);
}

.new-here {
  margin: 0;
  font-size: 14px;
  color: var(--muted);
  line-height: 1.5;
}

.new-here a {
  color: var(--teal-text);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.hint {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}

.hint code {
  background: var(--surface);
  padding: 1px 5px;
  border-radius: 5px;
}
</style>
