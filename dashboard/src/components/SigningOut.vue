<script setup>
// The card shown between pressing Log out and the login screen coming back. App.vue
// keeps it up while the real /auth/logout call runs (and a little longer, so it never
// flashes). `failed` means the gateway could not be reached: the session is dropped on
// this device only, the spinner stops, and the sentence says so.
import StatusCard from './StatusCard.vue'

defineProps({
  failed: { type: Boolean, default: false },
})
</script>

<template>
  <StatusCard
    title="Signing you out"
    note="Your spend, keys, and findings stay saved. You'll see them again next time you sign in."
    failed-note="Couldn't reach the server, so you're signed out on this device only."
    :failed="failed"
  />
</template>
