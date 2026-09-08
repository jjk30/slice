<script setup>
// The card shown between pressing Log out and the login screen coming back. App.vue
// keeps it up while the real /auth/logout call runs (and a little longer, so it never
// flashes). `failed` means the gateway could not be reached: the session is dropped on
// this device only, the spinner stops, and the sentence says so.
defineProps({
  failed: { type: Boolean, default: false },
})
</script>

<template>
  <div class="login">
    <div class="login-card card" role="status" aria-live="polite">
      <div class="brand">
        <img class="brand-logo" src="/favicon.png" alt="" width="28" height="28" />
        <h1 class="brand-name">slice</h1>
      </div>
      <div class="title-row">
        <span class="spinner" :class="{ still: failed }" aria-hidden="true"></span>
        <h2 class="title">Signing you out</h2>
      </div>
      <p v-if="failed" class="note">Couldn't reach the server, so you're signed out on this device only.</p>
      <p v-else class="note">Your spend, keys, and findings stay saved. You'll see them again next time you sign in.</p>
    </div>
  </div>
</template>

<style scoped>
/* The same shell as LoginScreen: full-height paper, one centred card. */
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

.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.title {
  font-family: var(--display);
  font-weight: 500;
  font-size: 24px;
  line-height: 1.2;
  margin: 0;
  color: var(--ink);
}

/* An 18px ring with a teal top arc that turns; a still ring when motion is reduced or
   the sign-out has already settled. */
.spinner {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2.5px solid var(--line);
  border-top-color: var(--teal);
  animation: turn .8s linear infinite;
}

.spinner.still {
  animation: none;
}

@keyframes turn {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .spinner {
    animation: none;
  }
}

.note {
  margin: 0;
  font-family: var(--body);
  font-size: 15.5px;
  color: var(--muted);
  line-height: 1.55;
}
</style>
