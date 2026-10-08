<script setup lang="ts">
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useNavigation } from './app/useNavigation'
import { useTheme } from './app/useTheme'
import SessionStatus from './components/SessionStatus.vue'
import { useSession } from './session/useSession'
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

function backToList(): void {
  navigation.openList()
  window.history.replaceState(null, '', window.location.pathname)
}

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
  webApp?.BackButton.onClick(backToList)
  void session.signIn()
})

onBeforeUnmount(() => {
  webApp?.BackButton.offClick(backToList)
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
