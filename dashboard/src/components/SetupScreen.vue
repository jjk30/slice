<script setup>
import { ref, computed, onMounted } from 'vue'
import { getJson, getBudget, putBudget, AuthError } from '../api.js'
import { apiBase } from '../api.js'
import { session } from '../auth.js'
import awsLogo from '../assets/aws-logo.png'

// Phase 21: the first-time setup screen, shown once after sign-in while the account's
// profile_confirmed is still false. Two things: an email slice can reach the user on
// (required, saving it is what marks the profile confirmed), and an optional read-only
// AWS role so the scanner can scan the user's account. No WhatsApp field: the API still
// accepts whatsapp_number, this screen just does not ask.
// Phase 23: the same screen serves first-time onboarding and later editing. In
// 'settings' mode the copy changes and a "Back to dashboard" link emits 'close';
// the fields, validation, and save/connect calls stay identical.
const props = defineProps({
  mode: {
    type: String,
    default: 'onboarding',
  },
})
// Phase 25: 'budget-saved' carries the PUT /account/budget reply so the dashboard's
// Account budget panel can update its cap, used, left and bar without a reload.
const emit = defineEmits(['done', 'close', 'budget-saved'])

const isSettings = computed(() => props.mode === 'settings')

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

// Phase 25: the monthly budget cap, Settings only. The field holds the current cap
// (the config default until the user sets one); Save PUTs it and shows the reply.
const capInput = ref('')
const capIsDefault = ref(false)
const capLoaded = ref(false)
const capSaving = ref(false)
const capError = ref('')
const capNotice = ref('')

const capNumber = computed(() => {
  const raw = String(capInput.value).trim()
  if (raw === '') return NaN
  return Number(raw)
})
// Mirrors the gateway's rule so the button only enables for a value it will accept.
const capValid = computed(() => {
  const n = capNumber.value
  if (!Number.isFinite(n) || n < 1 || n > 10000) return false
  return Math.round(n * 100) === n * 100
})

async function loadBudget() {
  try {
    const b = await getBudget()
    capInput.value = typeof b.cap_usd === 'number' ? b.cap_usd.toFixed(2) : ''
    capIsDefault.value = Boolean(b.is_default)
    capLoaded.value = true
  } catch (e) {
    if (e instanceof AuthError) session.value = null
    capError.value = 'Could not read the current cap.'
  }
}

async function saveBudget() {
  if (!capValid.value || capSaving.value) return
  capSaving.value = true
  capError.value = ''
  capNotice.value = ''
  try {
    const reply = await putBudget(Number(capNumber.value.toFixed(2)))
    capInput.value = typeof reply.cap_usd === 'number' ? reply.cap_usd.toFixed(2) : capInput.value
    capIsDefault.value = Boolean(reply.is_default)
    capNotice.value = reply.message || 'Saved.'
    emit('budget-saved', reply)
  } catch (e) {
    if (e instanceof AuthError) return
    capError.value = e && e.message ? e.message : 'Could not save the cap. Try again.'
  } finally {
    capSaving.value = false
  }
}

// Read GET /scanner/connect and reflect it into the AWS block. Used on mount and
// again right after a successful connect so the status shows what the backend now sees.
async function loadConnect() {
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
}

onMounted(async () => {
  try {
    const profile = await getJson('/account/profile')
    if (profile.email) email.value = profile.email
  } catch (e) {
    // A load failure just leaves the email blank; the user can type one.
    if (e instanceof AuthError) session.value = null
  }
  await loadConnect()
  if (isSettings.value) await loadBudget()
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
    // Re-read the status from the backend rather than assuming success, so the
    // status line reflects what the scanner actually sees after the assume-role.
    await loadConnect()
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
      <h2 class="title">{{ isSettings ? 'Settings' : 'Two quick things' }}</h2>
      <p class="sub">
        {{ isSettings ? 'Update the email and AWS role slice uses.' : 'So slice can reach you when it matters.' }}
      </p>

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
        <p class="aws-status">
          Status:
          <span :class="connected ? 'aws-ok' : 'aws-muted'">{{ connected ? 'connected' : 'not connected' }}</span>
        </p>
        <p class="aws-lede">
          Link a read-only role and slice shows your cloud bill next to your AI spend. It
          only reads your account name and costs. It never sees your keys, your data, or
          anything that could change your bill.
        </p>
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
        ><img class="aws-create-logo" :src="awsLogo" alt="AWS" />Create the read-only role in AWS</a>
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
        <p v-if="connectError" class="aws-err" role="alert">{{ connectError }}</p>
      </section>

      <section v-if="isSettings" class="budget">
        <label class="field">
          <span class="label">Monthly budget cap<span v-if="capLoaded && capIsDefault" class="cap-default"> (default)</span></span>
          <div class="key-row">
            <span class="cap-prefix mono">$</span>
            <input
              v-model="capInput"
              type="number"
              inputmode="decimal"
              min="1"
              max="10000"
              step="0.01"
              class="input mono"
              placeholder="25.00"
              aria-label="Monthly budget cap in dollars"
            />
            <button type="button" class="connect" :disabled="!capValid || capSaving" @click="saveBudget">
              {{ capSaving ? 'Saving' : 'Save' }}
            </button>
          </div>
        </label>
        <p class="aws-lede">slice blocks your requests when spend reaches this. It warns you by email at 80%.</p>
        <p v-if="capNotice" class="cap-ok" role="status">{{ capNotice }}</p>
        <p v-if="capError" class="aws-err" role="alert">{{ capError }}</p>
      </section>

      <p v-if="saveError" class="aws-err" role="alert">{{ saveError }}</p>
      <button type="button" class="submit" :disabled="!emailValid || saving" @click="saveAndContinue">
        {{ saving ? 'Saving…' : (isSettings ? 'Save' : 'Save and continue') }}
      </button>
      <a v-if="showAws && !isSettings" class="later" href="#" @click.prevent="saveAndContinue">Connect later</a>
      <a v-if="isSettings" class="later" href="#" @click.prevent="emit('close')">Back to dashboard</a>
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

.budget {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: var(--s2) 0 0;
  border-top: 1px solid var(--line);
}

.cap-prefix {
  align-self: center;
  color: var(--muted);
  font-size: 13px;
}

.cap-default {
  color: var(--muted);
}

.cap-ok {
  margin: 0;
  font-size: 12px;
  color: var(--teal);
}

.aws-lede {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}

.aws-create {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 13px;
  text-decoration: none;
}

/* The AWS mark, used as-is: fixed height, width follows the aspect ratio (no stretch). */
.aws-create-logo {
  height: 18px;
  width: auto;
  display: block;
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

.aws-status {
  margin: 0;
  font-size: 13px;
  color: var(--muted);
}

.aws-ok {
  margin: 0;
  font-size: 13px;
  color: var(--teal);
}

.aws-muted {
  color: var(--muted);
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
