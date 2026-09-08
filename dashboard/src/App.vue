<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getJson, getAwsCost, getScannerConnect, getFindings, AuthError } from './api.js'
import { useLiveEvents } from './live.js'
import { session, loadSession, logout, rememberLogin } from './auth.js'
import { money, dollars, percent, integer } from './format.js'
import KpiTile from './components/KpiTile.vue'
import LivePill from './components/LivePill.vue'
import TeamBudgets from './components/TeamBudgets.vue'
import ModelsChart from './components/ModelsChart.vue'
import RecentCalls from './components/RecentCalls.vue'
import GuardrailsTile from './components/GuardrailsTile.vue'
import FindingsPanel from './components/FindingsPanel.vue'
import SliceKeyCard from './components/SliceKeyCard.vue'
import LoginScreen from './components/LoginScreen.vue'
import SetupScreen from './components/SetupScreen.vue'
import SigningOut from './components/SigningOut.vue'
import Saving from './components/Saving.vue'
import FirstRequestCard from './components/FirstRequestCard.vue'
import awsMark from './assets/aws-mark.svg'

// Phase 21: the dashboard is locked behind a GitHub session (an httpOnly cookie). On
// mount we ask /auth/me who we are. No session -> the login screen. A session whose
// profile is not yet confirmed -> the one-time setup screen. Otherwise the dashboard.
// A 401 anywhere clears the session (see api.js) and flips back to login.
// The header logo. Built from Vite's BASE_URL ('./', see vite.config.js) so it stays
// relative and resolves wherever the dashboard is served from.
const logoSrc = import.meta.env.BASE_URL + 'favicon.png'

// `booted` gates the first render until /auth/me has answered, so the login screen never
// flashes before we know there is (or isn't) a session.
const booted = ref(false)
const profileConfirmed = ref(false)
// Phase 23: a confirmed user can reopen the setup screen to edit their email + AWS role.
const settingsOpen = ref(false)

// Phase 30: the two pages have paths. The dashboard is /dashboard and Settings is
// /settings, so a link or the back button lands on the right one. Only those two views
// move the path: login, setup, signing out and saving leave it alone.
const DASHBOARD_PATH = '/dashboard'
const SETTINGS_PATH = '/settings'

function currentPath() {
  return window.location.pathname
}

function pushPath(path) {
  if (currentPath() !== path) window.history.pushState({}, '', path)
}

function openSettings() {
  settingsOpen.value = true
  pushPath(SETTINGS_PATH)
}

// The back and forward buttons: the path decides which of the two pages shows, but only
// while the dashboard is the thing on screen (never mid sign-out or mid save).
function onPopState() {
  if (!session.value || !profileConfirmed.value || signingOut.value || savingSettings.value) return
  settingsOpen.value = currentPath() === SETTINGS_PATH
}
// Phase 22a: true right after a Log out, so the login screen shows a "Signed out" note.
const signedOut = ref(false)
// True from the moment Log out is pressed until the sign-out card has been up long enough
// to read; `logoutFailed` flips the card's wording when the gateway could not be reached.
const signingOut = ref(false)
const logoutFailed = ref(false)
// True from Settings' Save until the dashboard has been refetched and the saving card
// has been up long enough to read.
const savingSettings = ref(false)
const view = computed(() => {
  if (signingOut.value) return 'signing-out'
  if (savingSettings.value) return 'saving'
  if (!session.value) return 'login'
  if (!profileConfirmed.value) return 'setup'
  if (settingsOpen.value) return 'settings'
  return 'dashboard'
})
const accountLogin = computed(() => session.value?.login ?? null)

// Begin the dashboard's own loads and the live stream. Called once a confirmed session
// exists (on boot, or right after the setup screen finishes).
function startDashboard() {
  signedOut.value = false
  recentLoaded = false
  loadAll().finally(() => { initialLoadDone = true })
  startLive()
}

async function loadProfileConfirmed() {
  try {
    const profile = await getJson('/account/profile')
    profileConfirmed.value = Boolean(profile.profile_confirmed)
  } catch (e) {
    // A rejected session drops to login; any other read failure just shows setup.
    if (e instanceof AuthError) session.value = null
    profileConfirmed.value = false
  }
}

function onSetupDone() {
  profileConfirmed.value = true
  error.value = ''
  startDashboard()
}

// How long the saving card stays up at least, from when it appeared, so it never flashes.
const SAVE_MIN_MS = 1500

// Settings' one Save has written the email and the cap. Show the saving card, refetch
// everything the dashboard shows in one go (summary, models, teams, AWS cost, AWS
// connection status, findings, recent calls), hold the card to its floor, then show
// the dashboard. A save that failed never gets here: Settings stays up with the error
// under its field. A refetch that fails lands in the existing error line (loadAll
// marks it), and the dashboard shows after the hold all the same. The user stays
// confirmed and the live stream keeps flowing, so nothing is restarted.
async function onSettingsDone() {
  if (savingSettings.value) return
  const shownAt = Date.now()
  savingSettings.value = true
  settingsOpen.value = false
  pushPath(DASHBOARD_PATH)
  await loadAll()
  await wait(Math.max(0, SAVE_MIN_MS - (Date.now() - shownAt)))
  savingSettings.value = false
}

// Settings connected, reconnected or disconnected AWS. Refetch the three things that
// depend on it (the bill tile, the connection status that gates the findings panel, and
// the findings) so the dashboard is right the moment the user returns, no reload. A
// failed refetch keeps what was shown; the next live refresh retries.
async function refreshAws() {
  try {
    const [a, conn, f] = await Promise.all([getAwsCost(), getScannerConnect(), getFindings()])
    awsCost.value = a
    awsConn.value = conn
    findings.value = f
  } catch (e) {
    if (e instanceof AuthError) session.value = null
  }
}

// Phase 25: the cap was saved on the Settings screen. Patch the Account budget payload
// at once (cap, left, default flag) so the panel is right the moment it is shown again,
// then refetch /dashboard/teams for the per-model token lines. No page reload.
function onBudgetSaved(reply) {
  const t = teams.value
  if (t && typeof reply?.cap_usd === 'number') {
    const cap = reply.cap_usd
    const b = t.budget || {}
    const used = typeof b.budget_used_usd === 'number' ? b.budget_used_usd : 0
    teams.value = {
      ...t,
      budget_usd: cap,
      budget_default: Boolean(reply.is_default),
      budget: { ...b, budget_usd: cap, remaining_usd: Math.max(0, cap - used) },
    }
  }
  refreshTeams()
}

async function refreshTeams() {
  try {
    teams.value = await getJson('/dashboard/teams')
  } catch (e) {
    // A failed refresh keeps the patched payload; the next live event retries.
    if (e instanceof AuthError) session.value = null
  }
}

// How long the sign-out card stays up: at least this long from when it appeared when the
// gateway answered, and this long after a failed call so the wording can be read.
const SIGNOUT_MIN_MS = 4000
const SIGNOUT_FAILED_MS = 1500

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function onLogout() {
  if (signingOut.value) return
  // Show the sign-out card, close the live stream, then clear the cookie server-side, so
  // nothing keeps pulling the old session's data on the way out. The card is held up by
  // the real logout call: a floor of SIGNOUT_MIN_MS so it never flashes, or a longer
  // hold with the "this device only" wording when the gateway could not be reached.
  const shownAt = Date.now()
  logoutFailed.value = false
  signingOut.value = true
  stopLive()
  const reached = await logout()
  if (reached) {
    await wait(Math.max(0, SIGNOUT_MIN_MS - (Date.now() - shownAt)))
  } else {
    logoutFailed.value = true
    await wait(SIGNOUT_FAILED_MS)
  }
  profileConfirmed.value = false
  settingsOpen.value = false
  summary.value = models.value = teams.value = awsCost.value = recent.value = null
  awsConn.value = findings.value = null
  recentLoaded = false
  error.value = ''
  // `session` is already null, so once the card is released `view` flips to 'login';
  // this shows the "Signed out" note there. It is cleared the moment a sign-in starts a
  // fresh session (see startDashboard).
  signedOut.value = true
  signingOut.value = false
  logoutFailed.value = false
  // The next sign-in lands on the dashboard page, whichever page was open before.
  if (currentPath() !== DASHBOARD_PATH) window.history.replaceState({}, '', DASHBOARD_PATH)
}

const RECENT_LIMIT = 20
const REFRESH_DEBOUNCE_MS = 400

// Raw API payloads; null until the first successful load.
const summary = ref(null)
const models = ref(null)
const teams = ref(null)
const awsCost = ref(null)
// Phase 24b: the AWS connection status (decides whether the findings panel shows) and
// the newest run's findings.
const awsConn = ref(null)
const findings = ref(null)
const recent = ref(null)
const error = ref('')
// True after a load failed: cards render dashes instead of "Loading…".
const failed = computed(() => Boolean(error.value))

// ---- loading ---------------------------------------------------------------

// Monotonic sequence so an older in-flight refresh cannot overwrite a newer one.
// Only the refresh that actually applies its result may clear the error banner:
// a stale one finishing after a newer failure must not hide that failure.
let aggSeq = 0
// Whether /dashboard/recent has ever been loaded from the record book. Live rows
// alone don't count: until the DB list has been fetched the table is incomplete.
let recentLoaded = false

async function loadAggregates() {
  const seq = ++aggSeq
  const [s, m, t, a, conn, f] = await Promise.all([
    getJson('/dashboard/summary'),
    getJson('/dashboard/models'),
    getJson('/dashboard/teams'),
    getAwsCost(),
    getScannerConnect(),
    getFindings(),
  ])
  if (seq !== aggSeq) return false
  summary.value = s
  models.value = m
  teams.value = t
  awsCost.value = a
  awsConn.value = conn
  findings.value = f
  error.value = ''
  return true
}

function normalizeRecent(r) {
  return {
    key: `db:${r.id}`,
    created_at: r.created_at,
    team: r.team ?? null,
    model: r.model ?? null,
    routed_from: r.routed_from ?? null,
    status: r.status ?? null,
    cost: r.cost_usd ?? null,
    cached: Boolean(r.cached),
  }
}

async function loadRecent() {
  const body = await getJson(`/dashboard/recent?limit=${RECENT_LIMIT}`)
  const fetched = (body.requests || []).map(normalizeRecent)
  // Fetched (DB) rows are canonical and go first so their keys win; any live
  // rows that arrived while the fetch was in flight are kept unless they match
  // a DB row (see sameCall).
  recent.value = mergeRecent(fetched, recent.value || [])
  recentLoaded = true
}

// A failed load shows the banner and dashes: aggregates are cleared so no
// stale figure is presented as current. Live rows in `recent` are kept.
// An AuthError is not an outage: the key was rejected (api.js already cleared it),
// so fall through to the login screen instead of showing the banner.
function markFailed(e) {
  if (e instanceof AuthError) {
    summary.value = models.value = teams.value = awsCost.value = null
    awsConn.value = findings.value = null
    return
  }
  error.value = e && e.message ? e.message : 'Backend unavailable.'
  summary.value = null
  models.value = null
  teams.value = null
  awsCost.value = null
  awsConn.value = null
  findings.value = null
}

async function loadAll() {
  try {
    await Promise.all([loadAggregates(), loadRecent()])
  } catch (e) {
    markFailed(e)
  }
}

// The slice-key card rejected the session (401). api.js already cleared `session`, so
// `view` flips to the login screen on its own; nothing else to do but not treat it as
// an outage banner.
function onKeyAuthError() {}

// ---- recent list merging ----------------------------------------------------

function isDbRow(r) {
  return String(r.key).startsWith('db:')
}

// A live row (key = request_id) and its DB counterpart (key = db:N) describe
// the same call when every field matches and the timestamps are identical: the
// gateway stamps one instant on both the event and the Postgres row, at
// microsecond resolution, so equality is exact rather than a guessed window.
// Rows from the same source are only ever deduped by key.
function sameCall(a, b) {
  if (isDbRow(a) === isDbRow(b)) return false
  if (a.created_at !== b.created_at) return false
  if (a.team !== b.team || a.model !== b.model || a.status !== b.status) return false
  return a.routed_from === b.routed_from && a.cached === b.cached
}

function mergeRecent(existing, incoming) {
  const out = []
  const seen = new Set()
  for (const row of [...existing, ...incoming]) {
    if (seen.has(row.key)) continue
    if (out.some((o) => sameCall(o, row))) continue
    seen.add(row.key)
    out.push(row)
  }
  out.sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0))
  return out.slice(0, RECENT_LIMIT)
}

// ---- live events ------------------------------------------------------------

let refreshTimer = null

// Debounce: a burst of events triggers a single refresh ~400ms after the last one.
function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(async () => {
    refreshTimer = null
    try {
      // Also fetch /recent if the record-book list never loaded (e.g. Postgres was
      // down at mount): live rows alone would make the table look complete.
      await Promise.all([loadAggregates(), recentLoaded ? Promise.resolve() : loadRecent()])
    } catch (e) {
      markFailed(e)
    }
  }, REFRESH_DEBOUNCE_MS)
}

const { status: liveStatus, start: startLive, stop: stopLive } = useLiveEvents(
  (row) => {
    recent.value = mergeRecent([row], recent.value || [])
    scheduleRefresh()
  },
  // The stream (re)opened. After an outage events may have been missed, so resync.
  // On the very first open only resync if the mount-time load already finished and
  // failed (or never got the record-book list), the page may have been opened while
  // the gateway was still starting. If that load is still in flight there is nothing
  // to refetch yet; if it later fails, the next event's refresh retries.
  (firstOpen) => {
    if (!firstOpen) loadAll()
    else if (initialLoadDone && (error.value || !recentLoaded)) loadAll()
  },
)

let initialLoadDone = false
onMounted(async () => {
  // Ask who we are. With a confirmed session, start the dashboard; otherwise the login
  // or setup screen shows and takes it from there.
  await loadSession()
  if (session.value) {
    // A session is here (a fresh sign-in lands back on this page, so this covers that
    // too): remember its GitHub username for the sign-in card's "Log in as" button.
    rememberLogin(session.value.login)
    await loadProfileConfirmed()
    if (profileConfirmed.value) {
      // Phase 30: a valid, confirmed session opened at /settings lands on Settings;
      // any other path is the dashboard.
      settingsOpen.value = currentPath() === SETTINGS_PATH
      startDashboard()
    } else {
      initialLoadDone = true
    }
  } else {
    initialLoadDone = true
  }
  booted.value = true
  window.addEventListener('popstate', onPopState)
})
onBeforeUnmount(() => {
  window.removeEventListener('popstate', onPopState)
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
})

// ---- derived display values -----------------------------------------------

const month = computed(() => summary.value?.month ?? teams.value?.month ?? models.value?.month ?? null)

// null while loading; a dash once a load has failed.
function kpi(fmt) {
  if (summary.value) return fmt(summary.value)
  return failed.value ? '\u2014' : null
}

const spend = computed(() => kpi((s) => money(s.spend_usd)))
// Spend only sums costs the pricing table knows. When served requests carried an
// unknown price, say so next to the total rather than let it read as complete.
const spendSub = computed(() => {
  const n = summary.value?.unpriced_requests
  if (typeof n !== 'number' || n <= 0) return ''
  return `${integer(n)} served ${n === 1 ? 'request' : 'requests'} unpriced, not included`
})
const saved = computed(() => kpi((s) => money(s.savings_usd)))
const requests = computed(() => kpi((s) => integer(s.requests)))
const requestsSub = computed(() => {
  if (!summary.value) return ''
  return `${integer(summary.value.cache_hits)} cached · ${integer(summary.value.routed_down)} routed down`
})
const passRate = computed(() => kpi((s) => percent(s.eval?.pass_rate)))
const passRateSub = computed(() => {
  if (!summary.value) return ''
  return `${integer(summary.value.eval?.count)} scored`
})
// The AWS bill tile. Amounts arrive as decimal strings of dollars and cents, so they
// are shown with exactly two decimals (not the small-amount AI-spend formatter). A
// null month_to_date means the account has not connected AWS (or nothing has been
// fetched yet), which is said in words rather than shown as a $0 bill.
function usd(raw) {
  if (raw === null || raw === undefined) return null
  return dollars(Number(raw))
}
const awsConnected = computed(() => usd(awsCost.value?.month_to_date) !== null)
const awsBill = computed(() => {
  if (awsCost.value) return awsConnected.value ? usd(awsCost.value.month_to_date) : 'not connected'
  return failed.value ? '\u2014' : null
})
const awsBillSub = computed(() => {
  if (!awsCost.value) return ''
  if (!awsConnected.value) return 'connect AWS in Settings'
  const y = usd(awsCost.value.yesterday)
  return y === null ? '' : `yesterday ${y}`
})
// Phase 24b: the findings panel shows once AWS is connected. Phase 29: the operator is
// no exception, GET /scanner/connect reports its own switch as connected or
// not_connected like everyone else. Before the status is known it stays hidden.
const awsPanelVisible = computed(() => {
  const c = awsConn.value
  return Boolean(c && c.status === 'connected')
})
// A loaded summary with no guardrails object renders dashes, not "Loading…".
const guardrails = computed(() => (summary.value ? summary.value.guardrails ?? {} : null))
// The month's request count once the summary has loaded; null while loading or after a
// failed fetch, so the first-request card never shows on an unknown count. A count of
// zero puts that card above the tiles, holds the tiles back, and hides the two panels
// that would only be empty. It leaves on its own when a refresh or the live stream
// brings the count above zero.
const requestCount = computed(() => {
  if (!summary.value || failed.value) return null
  const n = summary.value.requests
  return typeof n === 'number' ? n : null
})
const firstRequest = computed(() => requestCount.value === 0)
</script>

<template>
  <template v-if="!booted" />
  <SigningOut v-else-if="view === 'signing-out'" :failed="logoutFailed" />
  <Saving v-else-if="view === 'saving'" />
  <LoginScreen v-else-if="view === 'login'" :signed-out="signedOut" />
  <SetupScreen v-else-if="view === 'setup'" mode="onboarding" @done="onSetupDone" />
  <SetupScreen
    v-else-if="view === 'settings'"
    mode="settings"
    @done="onSettingsDone"
    @budget-saved="onBudgetSaved"
    @aws-changed="refreshAws"
  />
  <div v-else class="page">
    <header class="header">
      <div class="brand">
        <img class="brand-logo" :src="logoSrc" alt="" width="24" height="24" />
        <h1 class="brand-name">slice</h1>
        <span class="brand-path">/ dashboard</span>
      </div>
      <div class="header-right">
        <span class="meta">this month · {{ month ?? '\u2014' }}</span>
        <span v-if="accountLogin" class="meta account">{{ accountLogin }}</span>
        <LivePill :status="liveStatus" />
        <a class="settings howto" href="https://sliceapp.dev/how-to" target="_blank" rel="noopener">How to</a>
        <button class="settings" type="button" @click="openSettings">Settings</button>
        <button class="signout" type="button" @click="onLogout">Log out</button>
      </div>
    </header>

    <div v-if="error" class="banner" role="alert">
      Backend unavailable: {{ error }}
    </div>

    <main class="grid">
      <FirstRequestCard class="span-4" :requests="requestCount" />
      <div class="kpis" :class="{ dimmed: firstRequest }">
        <KpiTile label="spend this month" :value="spend" :sub="spendSub" :failed="failed" tone="cherry" tint="lavender" />
        <KpiTile label="bill this month" :icon="awsMark" icon-alt="AWS" :value="awsBill" :sub="awsBillSub" :failed="failed" tint="white" />
        <KpiTile label="saved this month" :value="saved" :failed="failed" tone="teal" tint="green" />
        <KpiTile label="requests" :value="requests" :sub="requestsSub" :failed="failed" tint="bluegrey" />
        <KpiTile label="eval pass rate" :value="passRate" :sub="passRateSub" :failed="failed" tint="amber" />
      </div>

      <TeamBudgets class="span-2" :data="teams" :failed="failed" />
      <ModelsChart v-if="!firstRequest" class="span-2" :data="models" :failed="failed" />

      <FindingsPanel v-if="awsPanelVisible" class="span-4" :data="findings" :failed="failed" />

      <RecentCalls v-if="!firstRequest" class="span-4" :rows="recent" :failed="failed" />

      <GuardrailsTile :guardrails="guardrails" :failed="failed" />
      <SliceKeyCard class="span-2" @auth-error="onKeyAuthError" />
    </main>
  </div>
</template>
