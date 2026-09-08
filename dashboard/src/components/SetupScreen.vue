<script setup>
import { ref, computed, onMounted } from 'vue'
import { getJson, getBudget, putBudget, AuthError } from '../api.js'
import { apiBase } from '../api.js'
import { session } from '../auth.js'
import awsLogo from '../assets/aws-logo.png'
import awsMark from '../assets/aws-mark.svg'

// Phase 21: the first-time setup screen, shown once after sign-in while the account's
// profile_confirmed is still false. Two things: an email slice can reach the user on
// (required, saving it is what marks the profile confirmed), and an optional read-only
// AWS role so the scanner can scan the user's account. No WhatsApp field: the API still
// accepts whatsapp_number, this screen just does not ask.
// Phase 23: the same screen serves first-time onboarding and later editing. In
// 'settings' mode the copy changes and the one Save writes the email and the budget
// cap together, then emits 'done' so the dashboard comes back on its own; the fields,
// validation, and connect calls stay identical.
// Phase 29: the AWS block shows for every account. A connected account (the operator
// scanning slice's own account included) sees the status, what is being scanned, and a
// Disconnect button behind a one-line confirm. A not-connected account sees the connect
// flow; for the operator that is a single Reconnect button, no role needed.
// `connectInfo` is an optional GET /scanner/connect payload applied before the mount
// fetch (the server renderer never mounts), so the block can be rendered from a known
// state; the fetch on mount still refreshes it.
const props = defineProps({
  mode: {
    type: String,
    default: 'onboarding',
  },
  connectInfo: {
    type: Object,
    default: null,
  },
})
// Phase 25: 'budget-saved' carries the PUT /account/budget reply so the dashboard's
// Account budget panel can update its cap, used, left and bar without a reload.
const emit = defineEmits(['done', 'budget-saved'])

const isSettings = computed(() => props.mode === 'settings')

// A loose email check, just to enable the button; the gateway validates for real.
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/

const email = ref('')
const saving = ref(false)
const saveError = ref('')

// AWS connect state. `awsMode` is 'connect' for a normal account, 'operator' for the one
// that scans slice's own infrastructure, null until GET /scanner/connect has answered
// (or when it could not be read, in which case the block stays hidden).
const awsMode = ref(null)
const quickCreateUrl = ref('')
const roleArn = ref('')
const connecting = ref(false)
const connectError = ref('')
const connected = ref(false)
// Phase 29: the Disconnect flow. The confirm is one inline line, then the DELETE.
const confirmDisconnect = ref(false)
const disconnecting = ref(false)

const emailValid = computed(() => EMAIL_RE.test(email.value.trim()))
const showAws = computed(() => awsMode.value !== null)
const isOperator = computed(() => awsMode.value === 'operator')
// What a connected account is scanning: slice's own account, or the user's role.
const scanningLine = computed(() =>
  isOperator.value
    ? "Scanning slice's own AWS account once a day."
    : `Scanning ${roleArn.value || 'your AWS account'} once a day.`,
)

function applyConnect(info) {
  awsMode.value = info.mode === 'operator' ? 'operator' : 'connect'
  quickCreateUrl.value = info.quick_create_url || ''
  if (info.role_arn) roleArn.value = info.role_arn
  connected.value = info.status === 'connected'
  confirmDisconnect.value = false
}

if (props.connectInfo) applyConnect(props.connectInfo)

// Phase 25: the monthly budget cap, Settings only. The field holds the current cap
// (the config default until the user sets one). The screen's one Save PUTs it along
// with the email when it has changed; `capLoadedValue` is what the field held on load.
const capInput = ref('')
const capIsDefault = ref(false)
const capLoaded = ref(false)
const capLoadedValue = ref('')
const capError = ref('')

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
// The cap only takes part in Save once it has loaded; until then (or if the read
// failed) Save writes the email alone rather than blocking on a field it cannot check.
const capChanged = computed(() => capLoaded.value && String(capInput.value).trim() !== capLoadedValue.value)
const canSave = computed(() => emailValid.value && !saving.value && !(capChanged.value && !capValid.value))

async function loadBudget() {
  try {
    const b = await getBudget()
    capInput.value = typeof b.cap_usd === 'number' ? b.cap_usd.toFixed(2) : ''
    capLoadedValue.value = String(capInput.value).trim()
    capIsDefault.value = Boolean(b.is_default)
    capLoaded.value = true
  } catch (e) {
    if (e instanceof AuthError) session.value = null
    capError.value = 'Could not read the current cap.'
  }
}

// PUT the cap when it changed. True when there was nothing to do or it saved; false
// (with the error shown) when the write failed, so Save stays on this screen.
async function saveBudgetIfChanged() {
  if (!capChanged.value) return true
  capError.value = ''
  try {
    const reply = await putBudget(Number(capNumber.value.toFixed(2)))
    capInput.value = typeof reply.cap_usd === 'number' ? reply.cap_usd.toFixed(2) : capInput.value
    capLoadedValue.value = String(capInput.value).trim()
    capIsDefault.value = Boolean(reply.is_default)
    emit('budget-saved', reply)
    return true
  } catch (e) {
    if (e instanceof AuthError) return false
    capError.value = e && e.message ? e.message : 'Could not save the cap. Try again.'
    return false
  }
}

// Read GET /scanner/connect and reflect it into the AWS block. Used on mount and
// again right after a successful connect so the status shows what the backend now sees.
async function loadConnect() {
  try {
    applyConnect(await getJson('/scanner/connect'))
  } catch (e) {
    // Treat an unreadable scanner as "no AWS block" rather than blocking setup.
    if (e instanceof AuthError) session.value = null
    awsMode.value = null
  }
}

// One call shape for the three AWS actions: connect a role (POST {role_arn}), the
// operator's reconnect (POST with an empty body), and disconnect (DELETE). Every one
// re-reads GET /scanner/connect afterwards rather than assuming, so the block shows what
// the scanner now sees.
async function awsCall(method, body) {
  const res = await fetch(apiBase() + '/scanner/connect', {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401) {
    session.value = null
    return false
  }
  if (!res.ok) {
    const reply = await res.json().catch(() => null)
    throw new Error((reply && reply.error && reply.error.message) || 'Could not update the AWS connection.')
  }
  await loadConnect()
  return true
}

async function reconnectOperator() {
  if (connecting.value) return
  connecting.value = true
  connectError.value = ''
  try {
    await awsCall('POST', {})
  } catch (e) {
    connectError.value = e && e.message ? e.message : 'Could not reach the gateway. Try again.'
  } finally {
    connecting.value = false
  }
}

async function disconnectAws() {
  if (disconnecting.value) return
  disconnecting.value = true
  connectError.value = ''
  try {
    await awsCall('DELETE')
  } catch (e) {
    connectError.value = e && e.message ? e.message : 'Could not reach the gateway. Try again.'
  } finally {
    disconnecting.value = false
    confirmDisconnect.value = false
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

// The one Save: the email (which is what confirms the profile on first-time setup),
// then in Settings the cap when it changed, then 'done' so the caller moves on. A
// failed cap write leaves the screen up with the error under the cap field.
async function saveAndContinue() {
  if (!canSave.value) return
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
    if (isSettings.value && !(await saveBudgetIfChanged())) return
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
        {{ isSettings ? 'Update the email slice uses and your AWS connection.' : 'So slice can reach you when it matters.' }}
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
        <span class="label aws-label"><img class="aws-mark" :src="awsMark" alt="AWS" /></span>
        <p class="aws-status">
          Status:
          <span :class="connected ? 'aws-ok' : 'aws-muted'">{{ connected ? 'connected' : 'not connected' }}</span>
        </p>

        <!-- Connected: what is being scanned, and Disconnect behind a one-line confirm. -->
        <template v-if="connected">
          <p class="aws-lede aws-scanning">{{ scanningLine }}</p>
          <div v-if="confirmDisconnect" class="key-row aws-confirm" role="group" aria-label="Confirm disconnect">
            <span class="aws-lede">Stop scanning AWS? Your AI spend data stays.</span>
            <button type="button" class="connect aws-danger" :disabled="disconnecting" @click="disconnectAws">
              {{ disconnecting ? 'Disconnecting' : 'Yes, disconnect' }}
            </button>
            <button type="button" class="connect" :disabled="disconnecting" @click="confirmDisconnect = false">Keep</button>
          </div>
          <button v-else type="button" class="connect aws-disconnect" @click="confirmDisconnect = true">Disconnect</button>
        </template>

        <!-- The operator, switched off: one Reconnect, no role needed. -->
        <template v-else-if="isOperator">
          <p class="aws-lede">slice scans its own AWS account. No role needed.</p>
          <button type="button" class="connect aws-reconnect" :disabled="connecting" @click="reconnectOperator">
            {{ connecting ? 'Reconnecting' : 'Reconnect' }}
          </button>
        </template>

        <!-- Everyone else, not connected: the role flow as before. -->
        <template v-else>
          <p class="aws-lede">
            Optional. Link a read-only role and slice shows your cloud bill next to your AI
            spend. It only reads your account name and costs. It never sees your keys, your
            data, or anything that could change your bill.
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
        </template>
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
          </div>
        </label>
        <p class="aws-lede">slice blocks your requests when spend reaches this. It warns you by email at 80%.</p>
        <p v-if="capChanged && !capValid" class="aws-err" role="alert">Enter a whole dollar or cent amount from 1.00 to 10000.00.</p>
        <p v-if="capError" class="aws-err" role="alert">{{ capError }}</p>
      </section>

      <p v-if="saveError" class="aws-err" role="alert">{{ saveError }}</p>
      <button type="button" class="submit" :disabled="!canSave" @click="saveAndContinue">
        {{ saving ? 'Saving…' : (isSettings ? 'Save' : 'Save and continue') }}
      </button>
      <a v-if="showAws && !connected && !isOperator && !isSettings" class="later" href="#" @click.prevent="saveAndContinue">Connect later</a>
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

/* Phase 29: the AWS actions sit on their own line; the confirm wraps on a narrow card. */
.aws-disconnect,
.aws-reconnect {
  align-self: flex-start;
  padding: 8px 12px;
}

.aws-confirm {
  align-items: center;
  flex-wrap: wrap;
}

.aws-confirm .connect {
  padding: 6px 12px;
}

.aws-danger {
  color: var(--cherry-text);
}

.aws-scanning {
  color: var(--ink);
}

/* The AWS mark beside the block's label: 22px tall, width from its own aspect ratio. */
.aws-label {
  display: inline-flex;
  align-items: center;
  gap: 9px;
}

.aws-mark {
  height: 22px;
  width: auto;
  display: block;
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
