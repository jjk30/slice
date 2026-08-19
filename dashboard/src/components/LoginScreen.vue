<script setup>
import { ref } from 'vue'

// Phase 12: the plain slice-key bridge. The full GitHub web login is a later step; for
// now the dashboard just takes a slice key (from `slice login`) and holds it in
// sessionStorage. Emits `login` with the trimmed key; App.vue stores it and loads.
const emit = defineEmits(['login'])
const key = ref('')
const show = ref(false)

function submit() {
  const trimmed = key.value.trim()
  if (trimmed) emit('login', trimmed)
}
</script>

<template>
  <div class="login">
    <form class="login-card card" @submit.prevent="submit">
      <div class="brand">
        <img class="brand-logo" src="/favicon.png" alt="" width="28" height="28" />
        <h1 class="brand-name">slice</h1>
      </div>
      <p class="lede">Sign in to your dashboard with a slice key.</p>
      <label class="field">
        <span class="label">slice key</span>
        <div class="key-row">
          <input
            :type="show ? 'text' : 'password'"
            v-model="key"
            class="mono input"
            placeholder="slk_live_…"
            autocomplete="off"
            spellcheck="false"
            aria-label="slice key"
          />
          <button type="button" class="reveal" @click="show = !show">{{ show ? 'hide' : 'show' }}</button>
        </div>
      </label>
      <button type="submit" class="submit" :disabled="!key.trim()">Continue</button>
      <p class="hint">
        Run <code class="mono">slice login</code> in your terminal to get a key. It is kept only
        for this browser tab. Full GitHub sign-in for the dashboard lands in a later step.
      </p>
    </form>
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

.submit {
  padding: 11px 14px;
  border: 0;
  border-radius: 10px;
  background: var(--teal);
  color: #fff;
  font-weight: 500;
  font-size: 14px;
  cursor: pointer;
}

.submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
