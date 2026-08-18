<script setup>
// SSE connection status: 'live' | 'reconnecting' | 'offline'. Teal dot when
// connected, muted otherwise; the word is always there, never colour alone.
defineProps({
  status: { type: String, required: true },
})
</script>

<template>
  <span class="pill" :class="`pill--${status}`" role="status" aria-live="polite">
    <span class="dot" aria-hidden="true"></span>
    <span>{{ status }}</span>
  </span>
</template>
