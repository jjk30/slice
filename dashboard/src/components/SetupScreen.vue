<script setup>
import { ref, computed, onMounted } from 'vue'
import { getJson, AuthError } from '../api.js'
import { apiBase } from '../api.js'
import { session } from '../auth.js'

// Phase 21: the first-time setup screen, shown once after sign-in while the account's
// profile_confirmed is still false. Two things: an email slice can reach the user on
// (required, saving it is what marks the profile confirmed), and an optional read-only
// AWS role so the scanner can scan the user's account. No WhatsApp field: the API still
// accepts whatsapp_number, this screen just does not ask.
const emit = defineEmits(['done'])

// A loose email check, just to enable the button; the gateway validates for real.
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/

const email = ref('')
const saving = ref(false)
const saveError = ref('')

// AWS connect state. `mode` is 'connect' for a normal account, 'operator' for the one that
// scans slice's own infrastructure (then the whole AWS block is hidden).
const awsMode = ref(null)
const quickCreateUrl = ref('')
const roleArn = ref('')
const connecting = ref(false)
const connectError = ref('')
const connected = ref(false)

const emailValid = computed(() => EMAIL_RE.test(email.value.trim()))
const showAws = computed(() => awsMode.value === 'connect')

onMounted(async () => {
  try {
    const profile = await getJson('/account/profile')
    if (profile.email) email.value = profile.email
  } catch (e) {
    // A load failure just leaves the email blank; the user can type one.
    if (e instanceof AuthError) session.value = null
  }
  try {
    const info = await getJson('/scanner/connect')
    awsMode.value = info.mode === 'operator' ? 'operator' : 'connect'
    quickCreateUrl.value = info.quick_create_url || ''
    if (info.role_arn) roleArn.value = info.role_arn
    connected.value = info.status === 'connected'
  } catch (e) {
    // Treat an unreadable scanner as "no AWS block" rather than blocking setup.
    awsMode.value = 'operator'
  }
})

async function saveAndContinue() {
  if (!emailValid.value || saving.value) return
  saving.value = true
  saveError.value = ''
  try {
    const res = await fetch(apiBase() + '/account/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ email: email.value.trim() }),
    })
    if (res.status === 401) {
      session.value = null
      return
    }
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      saveError.value = (body && body.error && body.error.message) || 'Could not save. Try again.'
      return
    }
    emit('done')
  } catch (e) {
    saveError.value = 'Could not reach the gateway. Try again.'
  } finally {
    saving.value = false
  }
}

async function connectAws() {
  const arn = roleArn.value.trim()
  if (!arn || connecting.value) return
  connecting.value = true
  connectError.value = ''
  try {
    const res = await fetch(apiBase() + '/scanner/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ role_arn: arn }),
    })
    if (res.status === 401) {
      session.value = null
      return
    }
    const body = await res.json().catch(() => null)
    if (!res.ok) {
      connectError.value = (body && body.error && body.error.message) || 'Could not verify the role. Check the ARN.'
      return
    }
    connected.value = true
  } catch (e) {
    connectError.value = 'Could not reach the gateway. Try again.'
  } finally {
    connecting.value = false
  }
}
</script>

<template>
  <div class="setup">
    <div class="setup-card card">
      <div class="brand">
        <img class="brand-logo" src="/favicon.png" alt="" width="28" height="28" />
        <h1 class="brand-name">slice</h1>
      </div>
      <h2 class="title">Two quick things</h2>
      <p class="sub">So slice can reach you when it matters.</p>

      <label class="field">
        <span class="label">Email</span>
        <input
          v-model="email"
          type="email"
          class="input"
          placeholder="you@example.com"
          autocomplete="email"
          spellcheck="false"
          aria-label="Email"
        />
      </label>

      <section v-if="showAws" class="aws">
        <span class="label">Connect AWS (optional)</span>
        <p class="aws-lede">
          Create a read-only role in your AWS account so slice can scan it. Nothing is
          changed in your account, the role only lets slice read.
        </p>
        <a
          v-if="quickCreateUrl"
          class="aws-create"
          :href="quickCreateUrl"
          target="_blank"
          rel="noopener"
        >Create the read-only role in AWS</a>
        <div class="key-row">
          <input
            v-model="roleArn"
            class="input mono"
            placeholder="arn:aws:iam::123456789012:role/slice-scanner"
            spellcheck="false"
            aria-label="Role ARN"
          />
          <button type="button" class="connect" :disabled="!roleArn.trim() || connecting" @click="connectAws">
            {{ connecting ? 'Connecting…' : 'Connect' }}
          </button>
        </div>
        <p v-if="connected" class="aws-ok">AWS account connected.</p>
        <p v-else-if="connectError" class="aws-err" role="alert">{{ connectError }}</p>
      </section>

      <p v-if="saveError" class="aws-err" role="alert">{{ saveError }}</p>
      <button type="button" class="submit" :disabled="!emailValid || saving" @click="saveAndContinue">
        {{ saving ? 'Saving…' : 'Save and continue' }}
      </button>
      <a v-if="showAws" class="later" href="#" @click.prevent="saveAndContinue">Connect later</a>
    </div>
  </div>
</template>

<style scoped>
.setup {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: var(--s3);
  background: var(--paper);
}

.setup-card {
  width: 100%;
  max-width: 420px;
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

.title {
  margin: 6px 0 0;
  font-size: 18px;
  color: var(--ink);
}

.sub {
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

.aws {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: var(--s2) 0 0;
  border-top: 1px solid var(--line);
}

.aws-lede {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}

.aws-create {
  align-self: flex-start;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 13px;
  text-decoration: none;
}

.connect {
  padding: 0 14px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  font-size: 13px;
}

.connect:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.aws-ok {
  margin: 0;
  font-size: 13px;
  color: var(--teal);
}

.aws-err {
  margin: 0;
  font-size: 13px;
  color: var(--cherry, #b3261e);
}

.submit {
  margin-top: var(--s2);
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

.later {
  align-self: center;
  font-size: 12px;
  color: var(--muted);
}
</style>
