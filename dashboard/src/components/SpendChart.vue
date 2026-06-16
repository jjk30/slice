<script setup>
import { computed } from "vue";
import { fmtDay } from "../format.js";

// Hand-built SVG line chart, matching mockups/dashboard.html's chart exactly:
// same viewBox/geometry, green stroke (#0F6E56), gradient fill, end-point dot.
// We omit the mockup's dashed "slice on" marker because there is no real
// slice-turned-on date to anchor it to — drawing one would be fabricated.
const props = defineProps({
  series: { type: Array, default: () => [] }, // [{ day, spendUsd, requests }]
});

const GEO = { W: 600, H: 170, pad: 10 };

const chart = computed(() => {
  const data = props.series.map((p) => p.spendUsd || 0);
  const n = data.length;
  if (n < 2) return null;

  const { W, H, pad } = GEO;
  const max = Math.max(...data, 0) || 1; // avoid divide-by-zero on an all-zero range
  const x = (i) => pad + ((W - 2 * pad) * i) / (n - 1);
  const y = (v) => H - 12 - ((H - 30) * (v - 0)) / (max - 0);

  let line = "";
  for (let i = 0; i < n; i++) line += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(data[i]).toFixed(1) + " ";
  const area = line + "L" + x(n - 1).toFixed(1) + " " + (H - 2) + " L" + x(0).toFixed(1) + " " + (H - 2) + " Z";

  return {
    line,
    area,
    cx: x(n - 1).toFixed(1),
    cy: y(data[n - 1]).toFixed(1),
    start: fmtDay(props.series[0].day),
    end: fmtDay(props.series[n - 1].day),
  };
});
</script>

<template>
  <template v-if="chart">
    <svg class="chart" viewBox="0 0 600 170" preserveAspectRatio="none" aria-label="Daily AI spend">
      <defs>
        <linearGradient id="spendgrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#0F6E56" stop-opacity="0.20" />
          <stop offset="1" stop-color="#0F6E56" stop-opacity="0" />
        </linearGradient>
      </defs>
      <path :d="chart.area" fill="url(#spendgrad)" />
      <path
        :d="chart.line"
        fill="none"
        stroke="#0F6E56"
        stroke-width="2.2"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
      <circle :cx="chart.cx" :cy="chart.cy" r="3.5" fill="#0F6E56" />
    </svg>
    <div class="clab"><span>{{ chart.start }}</span><span>{{ chart.end }}</span></div>
  </template>
</template>
