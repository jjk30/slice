<script setup>
// The one card shell the in-between screens share (signing out, saving settings): the
// same paper and centred card as LoginScreen, the brand row, a heading with the ring
// spinner to its left, and one sentence. `failed` swaps the sentence for `failedNote`
// and stops the spinner. The caller only supplies the words.
defineProps({
  title: { type: String, required: true },
  note: { type: String, required: true },
  failedNote: { type: String, required: true },
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
        <h2 class="title">{{ title }}</h2>
      </div>
      <p class="note">{{ failed ? failedNote : note }}</p>
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
   the card has settled on a failure. */
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
