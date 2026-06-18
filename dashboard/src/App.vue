<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { api } from "./api.js";
import Logo from "./components/Logo.vue";
import KpiCards from "./components/KpiCards.vue";
import SpendChart from "./components/SpendChart.vue";
import ModelBreakdown from "./components/ModelBreakdown.vue";
import RecentCalls from "./components/RecentCalls.vue";
import WhenToSwitch from "./components/WhenToSwitch.vue";

const DAYS = 30;
const REFRESH_MS = 20_000;

const loading = ref(true); // only true on the very first load
const error = ref("");
const summary = ref(null);
const daily = ref([]);
const models = ref([]);
const recent = ref([]);
const budgets = ref([]);
const suggestions = ref([]);
const totalPotentialUsd = ref(null);
const rules = ref([]);

// Switch-rules write state, owned here and passed down to the rules panel.
const rulesBusy = ref(false);
const rulesError = ref("");

let timer = null;

async function load(initial = false) {
  if (initial) loading.value = true;
  try {
    const [s, d, m, r, b, sug, rl] = await Promise.all([
      api.summary(DAYS),
      api.spendDaily(DAYS),
      api.spendByModel(DAYS),
      api.recent(8),
      api.budgets(),
      api.suggestions(DAYS),
      api.rules(),
    ]);
    summary.value = s;
    daily.value = d.series;
    models.value = m.models;
    recent.value = r.requests;
    budgets.value = b.teams;
    suggestions.value = sug.suggestions;
    totalPotentialUsd.value = sug.totalPotentialUsd;
    rules.value = rl;
    error.value = "";
  } catch (e) {
    error.value = e?.message || "Could not reach the gateway.";
  } finally {
    loading.value = false;
  }
}

// Save a rule, then re-fetch so the list reflects the source of truth. Returns
// true on success so the form can clear; on failure surfaces the gateway's
// message (e.g. a 400 for an unknown to_model) without crashing the dashboard.
async function saveRule(fromModel, toModel) {
  rulesBusy.value = true;
  rulesError.value = "";
  try {
    await api.saveRule(fromModel, toModel);
    rules.value = await api.rules();
    return true;
  } catch (e) {
    rulesError.value = e?.message || "Could not save the rule.";
    return false;
  } finally {
    rulesBusy.value = false;
  }
}

// Apply a recommendation: save its rule, then re-fetch BOTH the rules and the
// suggestions so the new rule appears in Current rules and the card updates.
async function applyRule(fromModel, toModel) {
  rulesBusy.value = true;
  rulesError.value = "";
  try {
    await api.saveRule(fromModel, toModel);
    const [rl, sug] = await Promise.all([api.rules(), api.suggestions(DAYS)]);
    rules.value = rl;
    suggestions.value = sug.suggestions;
    totalPotentialUsd.value = sug.totalPotentialUsd;
    return true;
  } catch (e) {
    rulesError.value = e?.message || "Could not apply the suggestion.";
    return false;
  } finally {
    rulesBusy.value = false;
  }
}

// Remove a rule, then re-fetch the list.
async function removeRule(fromModel) {
  rulesBusy.value = true;
  rulesError.value = "";
  try {
    await api.deleteRule(fromModel);
    rules.value = await api.rules();
    return true;
  } catch (e) {
    rulesError.value = e?.message || "Could not remove the rule.";
    return false;
  } finally {
    rulesBusy.value = false;
  }
}

// "No data yet" = the requests table is empty (recent is all-time, the most
// reliable signal of a fresh DB). With rows present we always show the
// dashboard, even if the selected window happens to be all-zeros.
const isEmpty = computed(() => !error.value && summary.value && recent.value.length === 0);
const ready = computed(() => !loading.value && !error.value && summary.value && !isEmpty.value);

// Real current month, e.g. "June 2026 · live".
const periodLabel = computed(
  () => new Date().toLocaleDateString("en-US", { month: "long", year: "numeric" }) + " · live",
);

onMounted(() => {
  load(true);
  timer = setInterval(() => load(false), REFRESH_MS);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <Logo />
        <b>slice</b><span class="sep">/</span><span class="t">dashboard</span>
      </div>
      <span class="period"><span class="d" />{{ periodLabel }}</span>
    </div>

    <!-- loading -->
    <div v-if="loading" class="state">
      <div class="spinner" />
      <div>Loading your spend…</div>
    </div>

    <!-- error -->
    <div v-else-if="error" class="state">
      <h2>Can't reach the gateway</h2>
      <p>{{ error }}</p>
      <code>Is slice running on the API base URL? (see dashboard/.env.example)</code>
    </div>

    <!-- empty: fresh DB, no requests yet -->
    <div v-else-if="isEmpty" class="state">
      <h2>No requests yet</h2>
      <p>Once traffic flows through slice, your spend, savings, and suggestions show up here.</p>
      <code>ANTHROPIC_BASE_URL=http://localhost:8080 claude</code>
    </div>

    <!-- dashboard -->
    <template v-else-if="ready">
      <KpiCards :summary="summary" :budgets="budgets" />

      <div class="grid">
        <div class="panel">
          <div class="ph"><h2>AI usage</h2><span class="meta">daily spend, last {{ summary.range.days }} days</span></div>
          <p class="sub">Daily spend, every request metered live as it passes through slice.</p>
          <SpendChart :series="daily" />
          <ModelBreakdown :models="models" />
          <RecentCalls :requests="recent" />
        </div>

        <WhenToSwitch
          :suggestions="suggestions"
          :total-potential-usd="totalPotentialUsd"
          :rules="rules"
          :rules-busy="rulesBusy"
          :rules-error="rulesError"
          :on-save="saveRule"
          :on-remove="removeRule"
          :on-apply="applyRule"
        />
      </div>
    </template>
  </div>
</template>
