<script setup>
import { computed, ref } from "vue";
import { usd } from "../format.js";

// Right-hand panel. Every card is a real, data-backed hint from /api/suggestions
// (see gateway/src/stats.ts for how each figure is derived). The mockup's
// non-functional "Apply" button is replaced by the hint's derivation note —
// this build can't auto-apply, and showing a dead button would be faking it.
//
// Below the hints: the "Current rules" panel — the team's switch-rules from
// /api/rules. on-save / on-remove are async handlers (App.vue) that hit the
// gateway and re-fetch; they resolve to a boolean so the add form knows whether
// to clear. v1 manages the default team (no team picker).
const props = defineProps({
  suggestions: { type: Array, default: () => [] },
  totalPotentialUsd: { type: Number, default: null },
  rules: { type: Array, default: () => [] },
  rulesBusy: { type: Boolean, default: false },
  rulesError: { type: String, default: "" },
  onSave: { type: Function, default: null },
  onRemove: { type: Function, default: null },
  onApply: { type: Function, default: null },
});

// accent (from the API) -> the mockup's card/tag colour variant.
const VARIANT = { green: "local", cherry: "tool", amber: "down", ink: "route" };

const cards = computed(() =>
  props.suggestions.map((s) => ({ ...s, variant: VARIANT[s.accent] || "route" })),
);

const ideaLabel = computed(() => {
  const n = props.suggestions.length;
  return `${n} idea${n === 1 ? "" : "s"}`;
});

const rulesLabel = computed(() => {
  const n = props.rules.length;
  return `${n} rule${n === 1 ? "" : "s"}`;
});

// Add-rule form state.
const fromModel = ref("");
const toModel = ref("");
const canSubmit = computed(
  () => !props.rulesBusy && fromModel.value.trim() !== "" && toModel.value.trim() !== "",
);

async function submitRule() {
  if (!canSubmit.value || !props.onSave) return;
  const ok = await props.onSave(fromModel.value.trim(), toModel.value.trim());
  if (ok) {
    fromModel.value = "";
    toModel.value = "";
  }
}

function removeRule(from) {
  if (props.rulesBusy || !props.onRemove) return;
  props.onRemove(from);
}

// Apply a recommendation: turn its from_model -> to_model into a saved rule,
// reusing the step-3a save path (errors surface in the same inline error line).
function applyCard(card) {
  if (props.rulesBusy || !props.onApply) return;
  props.onApply(card.from_model, card.to_model);
}
</script>

<template>
  <div class="panel">
    <div class="ph"><h2>When to switch</h2><span class="meta">{{ ideaLabel }}</span></div>
    <p class="sub">Cheaper ways to do the same work, found in your real usage.</p>

    <div class="empty-mini" v-if="!cards.length">
      Not enough data yet to suggest changes — hints appear as real traffic builds up.
    </div>

    <div class="switch" v-else>
      <div class="sc" :class="card.variant" v-for="card in cards" :key="card.id">
        <div class="sct">
          <h4>{{ card.title }}</h4>
          <span class="tag" :class="card.variant">{{ card.tag }}</span>
        </div>
        <p>{{ card.body }}</p>
        <div class="foot">
          <span class="save" v-if="card.saveUsd !== null">{{ usd(card.saveUsd) }}</span>
          <span v-else></span>
          <button
            v-if="card.from_model && card.to_model"
            class="act apply"
            type="button"
            :disabled="rulesBusy"
            :title="`Route ${card.from_model} → ${card.to_model}`"
            @click="applyCard(card)"
          >
            {{ rulesBusy ? "Applying…" : "Apply" }}
          </button>
          <span v-else class="note">{{ card.footer }}</span>
        </div>
      </div>
    </div>

    <!-- total potential strip — only when there are actionable opportunities -->
    <div class="totsave" v-if="totalPotentialUsd">
      Acting on the above would trim about <b>{{ usd(totalPotentialUsd) }}</b> more, on top of what slice
      already saves you.
    </div>

    <!-- current switch-rules: always route from one model to another -->
    <div class="rules">
      <div class="rules-head">
        <span class="rh-title">Current rules</span>
        <span class="rh-meta">{{ rulesLabel }}</span>
      </div>

      <div class="empty-mini" v-if="!rules.length">
        No switch rules yet — add one to always route a model to another.
      </div>
      <ul class="rule-list" v-else>
        <li class="rule-row" v-for="r in rules" :key="r.from_model">
          <span class="rule-map">
            <code>{{ r.from_model }}</code>
            <span class="arr" aria-hidden="true">→</span>
            <code class="to">{{ r.to_model }}</code>
          </span>
          <button
            class="rule-x"
            type="button"
            :disabled="rulesBusy"
            :aria-label="`Remove rule for ${r.from_model}`"
            :title="`Remove rule for ${r.from_model}`"
            @click="removeRule(r.from_model)"
          >
            ×
          </button>
        </li>
      </ul>

      <form class="rule-add" @submit.prevent="submitRule">
        <input
          class="rule-in"
          v-model="fromModel"
          :disabled="rulesBusy"
          placeholder="from model, e.g. claude-opus-4-8"
          spellcheck="false"
          autocomplete="off"
          aria-label="From model"
        />
        <input
          class="rule-in"
          v-model="toModel"
          :disabled="rulesBusy"
          placeholder="to model, e.g. gpt-4o"
          spellcheck="false"
          autocomplete="off"
          aria-label="To model"
        />
        <button class="rule-save" type="submit" :disabled="!canSubmit">
          {{ rulesBusy ? "Saving…" : "Save" }}
        </button>
      </form>

      <p class="rule-err" v-if="rulesError">{{ rulesError }}</p>
    </div>
  </div>
</template>
