<script setup lang="ts">
import { onMounted } from 'vue'
import SessionStatus from './components/SessionStatus.vue'
import { useSession } from './session/useSession'
import { startTelegram, telegramWebAppOf } from './telegram/useTelegram'

const props = defineProps<{ presetToken?: string }>()

const initData = startTelegram(telegramWebAppOf(window))
const session = useSession({
  baseUrl: window.location.origin,
  initData,
  ...(props.presetToken === undefined ? {} : { presetToken: props.presetToken }),
})

onMounted(() => session.signIn())
</script>

<template>
  <SessionStatus :state="session.state.value" />
</template>
