<script setup>
// The card shown between Settings' Save and the dashboard coming back. App.vue keeps it
// up while it refetches everything the dashboard shows (and at least a moment longer,
// so it never flashes). `failed` is for a save that did not go through: the spinner
// stops and the sentence says the form still holds the changes.
import StatusCard from './StatusCard.vue'

defineProps({
  failed: { type: Boolean, default: false },
})
</script>

<template>
  <StatusCard
    title="Saving your settings"
    note="Bringing your dashboard up to date so it shows what you just changed."
    failed-note="Couldn't save. Your changes are still on the form, try again."
    :failed="failed"
  />
</template>
