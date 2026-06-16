<script setup>
import { computed } from "vue";
import { usd } from "../format.js";

// Right-hand panel. Every card is a real, data-backed hint from /api/suggestions
// (see gateway/src/stats.ts for how each figure is derived). The mockup's
// non-functional "Apply" button is replaced by the hint's derivation note —
// this build can't auto-apply, and showing a dead button would be faking it.
const props = defineProps({
  suggestions: { type: Array, default: () => [] },
  totalPotentialUsd: { type: Number, default: null },
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
          <span class="note">{{ card.footer }}</span>
        </div>
      </div>
    </div>

    <!-- total potential strip — only when there are actionable opportunities -->
    <div class="totsave" v-if="totalPotentialUsd">
      Acting on the above would trim about <b>{{ usd(totalPotentialUsd) }}</b> more, on top of what slice
      already saves you.
    </div>
  </div>
</template>
