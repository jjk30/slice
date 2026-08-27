<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip } from 'chart.js'
import { money, integer, providerOf, providerLabel } from '../format.js'

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip)

// `data` is the /dashboard/models payload, or null while loading; `failed`
// means the load failed and a dash should replace "Loading…".
const props = defineProps({
  data: { type: Object, default: null },
  failed: { type: Boolean, default: false },
})

const models = computed(() => (props.data ? props.data.models || [] : null))
const rows = computed(() => (models.value || []).map((m) => ({
  label: m.model === null || m.model === undefined ? '(no model)' : m.model,
  provider: providerOf(m.model),
  providerName: providerLabel(m.model),
  requests: m.requests,
  spend: m.spend_usd,
  // Served requests on this model whose price is unknown: their spend is NOT in
  // `spend`, so the table and tooltip say so instead of implying they were free.
  unpriced: typeof m.unpriced_requests === 'number' ? m.unpriced_requests : 0,
})))

function unpricedNote(r) {
  if (!(r.unpriced > 0)) return ''
  return `${integer(r.unpriced)} unpriced`
}

const canvas = ref(null)
let chart = null // single Chart instance, reused across refreshes
let themeObserver = null // watches the manual html.dark class so colours follow it

// Panel meta, right of the title: how many models spent this month.
const meta = computed(() => {
  if (!models.value) return ''
  const n = models.value.length
  return `${integer(n)} ${n === 1 ? 'model' : 'models'}`
})

// Read theme colours and fonts from the CSS custom properties so the chart
// matches light/dark without duplicating the palette here. Spend is cherry.
function themeColors() {
  const cs = getComputedStyle(document.documentElement)
  return {
    accent: cs.getPropertyValue('--cherry').trim(),
    text2: cs.getPropertyValue('--muted').trim(),
    border: cs.getPropertyValue('--line').trim(),
    card: cs.getPropertyValue('--card').trim(),
    text: cs.getPropertyValue('--ink').trim(),
    mono: cs.getPropertyValue('--mono').trim() || 'monospace',
  }
}

function applyTheme() {
  if (!chart) return
  const c = themeColors()
  chart.data.datasets[0].backgroundColor = c.accent
  chart.options.scales.x.grid.color = c.border
  chart.options.scales.x.ticks.color = c.text2
  chart.options.scales.x.ticks.font = { family: c.mono, size: 11 }
  chart.options.scales.y.ticks.color = c.text2
  chart.options.scales.y.ticks.font = { family: c.mono, size: 11 }
  chart.options.plugins.tooltip.backgroundColor = c.card
  chart.options.plugins.tooltip.titleColor = c.text
  chart.options.plugins.tooltip.bodyColor = c.text2
  chart.options.plugins.tooltip.borderColor = c.border
  chart.options.plugins.tooltip.titleFont = { family: c.mono, size: 12 }
  chart.options.plugins.tooltip.bodyFont = { family: c.mono, size: 12 }
}

function buildChart() {
  if (!canvas.value || chart) return
  const c = themeColors()
  chart = new Chart(canvas.value, {
    type: 'bar',
    data: {
      labels: rows.value.map((r) => r.label),
      datasets: [{
        data: rows.value.map((r) => r.spend),
        backgroundColor: c.accent,
        barThickness: 14,
        borderRadius: 4,
        borderSkipped: 'start', // round only the data end of each bar
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          backgroundColor: c.card,
          titleColor: c.text,
          bodyColor: c.text2,
          borderColor: c.border,
          borderWidth: 1,
          padding: 10,
          cornerRadius: 8,
          titleFont: { family: c.mono, size: 12 },
          bodyFont: { family: c.mono, size: 12 },
          callbacks: {
            label(ctx) {
              const r = rows.value[ctx.dataIndex]
              if (!r) return ''
              const base = `${money(r.spend)} · ${integer(r.requests)} requests`
              return r.unpriced > 0 ? `${base} · ${unpricedNote(r)}` : base
            },
          },
        },
      },
      scales: {
        x: {
          beginAtZero: true,
          grid: { color: c.border, drawTicks: false },
          border: { display: false },
          ticks: { color: c.text2, callback: (v) => money(v), maxTicksLimit: 6, maxRotation: 0, font: { family: c.mono, size: 11 } },
        },
        y: {
          grid: { display: false },
          border: { display: false },
          ticks: {
            color: c.text2,
            autoSkip: false,
            font: { family: c.mono, size: 11 },
            // Long model ids would squeeze the plot; the full name is in the tooltip
            // title and in the table below.
            callback(value) {
              const label = this.getLabelForValue(value)
              // Shorter still on a narrow canvas, where the axis has little room.
              const limit = this.chart && this.chart.width < 420 ? 16 : 24
              return label.length > limit ? `${label.slice(0, limit - 1)}…` : label
            },
          },
        },
      },
    },
  })
}

function syncChart() {
  if (!chart) return
  chart.data.labels = rows.value.map((r) => r.label)
  chart.data.datasets[0].data = rows.value.map((r) => r.spend)
  applyTheme()
  chart.update()
}

// Height grows with the number of models so bars stay thin and readable.
const chartHeight = computed(() => {
  const n = rows.value.length
  return `${Math.max(1, n) * 28 + 32}px`
})

onMounted(() => {
  if (rows.value.length) buildChart()
  // The theme is not automatic (no prefers-color-scheme); a future manual toggle
  // adds/removes `dark` on <html>, so watch that class and re-read the tokens.
  themeObserver = new MutationObserver(() => { applyTheme(); if (chart) chart.update() })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

// The canvas only exists when there is data (v-if), so build lazily once the
// DOM has flushed, then keep updating the same instance; if the month goes
// empty the canvas is removed and the instance is dropped with it.
watch(rows, () => {
  if (rows.value.length === 0) {
    if (chart) chart.destroy()
    chart = null
    return
  }
  if (!chart) buildChart()
  else syncChart()
}, { flush: 'post' })

onBeforeUnmount(() => {
  if (themeObserver) themeObserver.disconnect()
  themeObserver = null
  if (chart) chart.destroy()
  chart = null
})
</script>

<template>
  <section class="card panel">
    <div class="panel-head">
      <h2 class="panel-title">Spend by model</h2>
      <span v-if="meta" class="meta">{{ meta }}</span>
    </div>
    <p v-if="models === null && failed" class="empty">—</p>
    <p v-else-if="models === null" class="loading">Loading…</p>
    <p v-else-if="models.length === 0" class="empty">No model spend this month yet.</p>
    <div v-else>
      <div class="chart-box" :style="{ height: chartHeight }">
        <canvas ref="canvas" role="img" aria-label="Horizontal bar chart of spend in USD per model this month; values are listed in the table below"></canvas>
      </div>
      <div class="table-wrap">
        <table class="models">
          <thead>
            <tr>
              <th scope="col">model</th>
              <th scope="col" class="num">requests</th>
              <th scope="col" class="num">spend</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.label">
              <td class="mono model-cell" :title="r.label">
                <span class="pdot" :class="`pdot--${r.provider}`" :title="r.providerName" aria-hidden="true"></span>{{ r.label }}
              </td>
              <td class="num mono">{{ integer(r.requests) }}</td>
              <td class="num mono">
                {{ money(r.spend) }}
                <span v-if="r.unpriced > 0" class="small unpriced" :title="`${unpricedNote(r)} served request(s) on this model carry no known price and are not in this spend`">{{ unpricedNote(r) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chart-box {
  position: relative;
  width: 100%;
  margin-bottom: 16px;
}

/* Fixed columns so the spend column never scrolls out of the card; long model
   ids ellipsize (full id in the title and the tooltip). */
.models {
  table-layout: fixed;
}

.models th:nth-child(1) { width: 56%; }
.models th:nth-child(2) { width: 18%; }
.models th:nth-child(3) { width: 26%; }

.model-cell {
  overflow: hidden;
  text-overflow: ellipsis;
}

.unpriced {
  display: block;
  white-space: nowrap;
  color: var(--amber-text);
  line-height: 1.3;
  margin-top: 2px;
}
</style>
