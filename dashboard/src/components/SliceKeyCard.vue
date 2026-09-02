<script setup>
import { ref, computed, onMounted } from 'vue'
import { getJson, postJson, AuthError } from '../api.js'

// Phase 22a: the "Your slice key" card. It reads GET /dashboard/key for the masked
// display of the account's live key (`slk_live_••••••••a1b2` plus the date it was
// minted), and "Create new key" POSTs /dashboard/key/rotate, which mints a fresh key
// and revokes the old one. The rotate reply carries the full plain key exactly once —
// we show it with a copy button until the user dismisses it, then only the masked row
// remains. The plain key is never stored and never re-fetched.
const emit = defineEmits(['auth-error'])

// The masked key: { prefix, last4, created_at } or null (no key yet). `null` while loading.
const card = ref(null)
const loaded = ref(false)
const loadFailed = ref(false)

// The full plain key from the most recent rotate, shown once. Null the rest of the time.
const revealed = ref(null)
const rotating = ref(false)
const rotateError = ref('')
const copied = ref(false)

const masked = computed(() => {
  if (!card.value) return ''
  const prefix = card.value.prefix || 'slk_live_'
  const last4 = card.value.last4 || ''
  return `${prefix}${'•'.repeat(8)}${last4}`
})

const createdLabel = computed(() => {
  const iso = card.value?.created_at
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
})

async function load() {
  try {
    const body = await getJson('/dashboard/key')
    card.value = body.key ?? null
    loadFailed.value = false
  } catch (e) {
    if (e instanceof AuthError) { emit('auth-error', e); return }
    loadFailed.value = true
  } finally {
    loaded.value = true
  }
}

async function rotate() {
  if (rotating.value) return
  rotating.value = true
  rotateError.value = ''
  copied.value = false
  try {
    const body = await postJson('/dashboard/key/rotate')
    revealed.value = body.slice_key
    card.value = body.key ?? card.value
  } catch (e) {
    if (e instanceof AuthError) { emit('auth-error', e); return }
    rotateError.value = e.message || 'Could not create a new key.'
  } finally {
    rotating.value = false
  }
}

async function copyKey() {
  if (!revealed.value) return
  try {
    await navigator.clipboard.writeText(revealed.value)
    copied.value = true
  } catch (e) {
    // Clipboard blocked (insecure origin, denied permission): leave the key selectable.
    copied.value = false
  }
}

// Drop the one-time plain key from memory once the user is done with it.
function dismiss() {
  revealed.value = null
  copied.value = false
}

onMounted(load)
</script>

<template>
  <section class="card panel key-card">
    <div class="panel-head">
      <h2 class="panel-title">Your slice key</h2>
    </div>

    <!-- The full key, shown once right after a rotate. -->
    <div v-if="revealed" class="reveal">
      <code class="mono key-full">{{ revealed }}</code>
      <div class="reveal-row">
        <button type="button" class="copy-btn" @click="copyKey">
          {{ copied ? 'Copied' : 'Copy' }}
        </button>
        <button type="button" class="dismiss-btn" @click="dismiss">Done</button>
      </div>
      <p class="warn">Copy it now. You won't see it again.</p>
    </div>

    <!-- Otherwise the masked row (or empty / loading / failed states). -->
    <template v-else>
      <p v-if="!loaded" class="loading">Loading…</p>
      <p v-else-if="loadFailed" class="empty">—</p>
      <template v-else-if="card">
        <code class="mono key-masked">{{ masked }}</code>
        <p v-if="createdLabel" class="meta created">Created {{ createdLabel }}</p>
      </template>
      <p v-else class="empty">No active key.</p>

      <p v-if="rotateError" class="key-error" role="alert">{{ rotateError }}</p>
      <button type="button" class="create-btn" :disabled="rotating" @click="rotate">
        {{ rotating ? 'Creating…' : 'Create new key' }}
      </button>
    </template>
  </section>
</template>

<style scoped>
.key-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.key-masked,
.key-full {
  font-size: 14px;
  color: var(--ink);
  word-break: break-all;
}

.key-full {
  display: block;
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
}

.created {
  margin: 0;
}

.reveal {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.reveal-row {
  display: flex;
  gap: 8px;
}

.warn {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}

.key-error {
  margin: 0;
  font-size: 13px;
  color: var(--cherry, #b3261e);
}

.create-btn {
  align-self: flex-start;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 13px;
  cursor: pointer;
}

.create-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.copy-btn {
  padding: 8px 14px;
  border: 0;
  border-radius: 10px;
  background: var(--teal);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.dismiss-btn {
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
}
</style>
