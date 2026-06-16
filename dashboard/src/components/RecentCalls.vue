<script setup>
import { usd, prettyModel } from "../format.js";

// We never store prompts/tasks, so the mockup's "task" column is filled with the
// routed model (the real, meaningful field). The middle column carries the live
// status/latency, and the right column the real cost.
defineProps({
  requests: { type: Array, default: () => [] },
});

function detail(r) {
  const head = r.cacheHit ? "cache hit" : r.verdict ? r.verdict : r.status;
  return `${head} · ${r.latencyMs}ms`;
}
</script>

<template>
  <div class="feed">
    <div class="fh">Recent calls</div>
    <div class="empty-mini" v-if="!requests.length">No requests logged yet.</div>
    <div class="frow" v-for="r in requests" :key="r.id">
      <span class="task">{{ prettyModel(r.model) }}</span>
      <span class="mdl">{{ detail(r) }}</span>
      <span class="c">{{ usd(r.costUsd) }}</span>
    </div>
  </div>
</template>
