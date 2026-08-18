<script setup>
// SSE connection status: 'live' | 'reconnecting' | 'offline'.
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
