<script setup lang="ts">
import { onBeforeUnmount, onMounted, provide, watch } from 'vue'
import { LEAVE_GUARD, createLeaveGuard, singleFlight } from './app/leaveGuard'
import { useNavigation } from './app/useNavigation'
import { useTheme } from './app/useTheme'
import SessionStatus from './components/SessionStatus.vue'
import { useSession } from './session/useSession'
import { confirmWith, setClosingConfirmation } from './telegram/actions'
import { startTelegram, telegramWebAppOf } from './telegram/useTelegram'
import PostEditor from './views/PostEditor.vue'
import PostList from './views/PostList.vue'

const props = defineProps<{ presetToken?: string }>()

const webApp = telegramWebAppOf(window)
const initData = startTelegram(webApp)
const stopTheme = useTheme(webApp, document.documentElement)
const session = useSession({
  baseUrl: window.location.origin,
  initData,
  ...(props.presetToken === undefined ? {} : { presetToken: props.presetToken }),
})
const navigation = useNavigation(window.location.search)

const leaveGuard = createLeaveGuard((dirty) => setClosingConfirmation(webApp, dirty))
provide(LEAVE_GUARD, leaveGuard)

// unsaved work asks first, once even on a double tap; «no» stays in the editor
const onBack = singleFlight(async () => {
  if (!(await leaveGuard.leave(confirmWith(webApp)))) return
  navigation.openList()
  window.history.replaceState(null, '', window.location.pathname)
})

// the Telegram back button exists only inside the editor and leads to the list
watch(
  navigation.inEditor,
  (inEditor) => {
    if (inEditor) webApp?.BackButton.show()
    else webApp?.BackButton.hide()
  },
  { immediate: true },
)

onMounted(() => {
  webApp?.BackButton.onClick(onBack)
  void session.signIn()
})

onBeforeUnmount(() => {
  webApp?.BackButton.offClick(onBack)
  stopTheme()
})
</script>

<template>
  <PostEditor
    v-if="session.state.value.kind === 'ready' && navigation.view.value.kind === 'editor'"
    :key="navigation.view.value.postId"
    :client="session.client"
    :post-id="navigation.view.value.postId"
    :web-app="webApp"
  />
  <PostList
    v-else-if="session.state.value.kind === 'ready'"
    :client="session.client"
    @open="navigation.openPost"
  />
  <SessionStatus v-else :state="session.state.value" />
</template>
