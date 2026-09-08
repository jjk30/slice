<script setup>
import { computed, ref } from 'vue'

// Shown above the KPI tiles when the account has sent nothing through slice yet.
// `requests` is the summary's count, or null while the summary is loading or after a
// fetch failed; the card renders only for an actual zero, and drops away on its own the
// moment a refresh or the live stream brings the count above zero.
const props = defineProps({
  requests: { type: Number, default: null },
})

const show = computed(() => props.requests === 0)

const LINES = [
  'export ANTHROPIC_BASE_URL=https://api.sliceapp.dev',
  'export ANTHROPIC_API_KEY=(your own Anthropic key)',
  'export ANTHROPIC_AUTH_TOKEN=(your slice key)',
]

const copied = ref(false)
let timer = null

async function copy() {
  try {
    await navigator.clipboard.writeText(LINES.join('\n'))
    copied.value = true
  } catch (e) {
    // Clipboard blocked (insecure origin, denied permission): the lines stay selectable.
    copied.value = false
  }
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { copied.value = false }, 1600)
}
</script>

<template>
  <section v-if="show" class="card panel first-request">
    <h2 class="panel-title">Send your first request through slice</h2>
    <p class="say">
      Nothing has come through yet. Paste these three lines in the terminal where you run
      Claude Code, then send any prompt. This page updates the moment it lands.
    </p>
    <div class="term">
      <button type="button" class="copy-btn" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button>
      <pre class="mono">export ANTHROPIC_BASE_URL=https://api.sliceapp.dev
export ANTHROPIC_API_KEY=<span class="ph">(your own Anthropic key)</span>
export ANTHROPIC_AUTH_TOKEN=<span class="ph">(your slice key)</span></pre>
    </div>
    <p class="say">
      <a class="setup-link" href="https://sliceapp.dev/how-to" target="_blank" rel="noopener">Full setup, step by step</a>
    </p>
    <p class="say">
      Or run <code class="mono">slice use claude-code</code> and it fills your key in for you.
    </p>
  </section>
</template>

<style scoped>
.first-request {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.say {
  margin: 0;
  font-size: 14px;
  color: var(--muted);
  line-height: 1.6;
  max-width: 60em;
}

/* The dark block from the how-to page: ink background, paper text, mono. */
.term {
  position: relative;
  background: var(--ink);
  color: var(--paper);
  border-radius: 12px;
  padding: 16px 18px;
  overflow-x: auto;
}

.term pre {
  margin: 0;
  font-size: 13px;
  line-height: 1.9;
  padding-right: 88px;
}

/* The two parts the reader fills in, in the how-to page's amber placeholder style. */
.ph {
  color: var(--selection);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}

.copy-btn {
  position: absolute;
  top: 12px;
  right: 12px;
  padding: 6px 12px;
  border: 0;
  border-radius: 10px;
  background: var(--teal);
  color: #fff;
  font-family: var(--body);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}

.setup-link {
  color: var(--teal-text);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.say code {
  background: var(--surface);
  padding: 1px 5px;
  border-radius: 5px;
  color: var(--ink);
  font-size: 12.5px;
}
</style>
