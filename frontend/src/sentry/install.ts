import {
  dedupeIntegration,
  globalHandlersIntegration,
  init,
  linkedErrorsIntegration,
  vueIntegration,
} from '@sentry/vue'
import type { App } from 'vue'
import { scrubEvent } from './scrub'

// no default integrations: they would read the URL, which carries Telegram's `initData` in its fragment
export function installSentry(app: App, page: Pick<Document, 'querySelector'>): void {
  const tag = page.querySelector('meta[name="sentry-dsn"]')
  const dsn = tag?.getAttribute('content')
  if (!tag || !dsn) return

  // set first: the SDK wraps an existing handler and calls it, instead of rethrowing
  app.config.errorHandler = (error) => console.error(error)
  init({
    dsn,
    environment: tag.getAttribute('data-environment') ?? undefined,
    release: tag.getAttribute('data-release') ?? undefined,
    defaultIntegrations: false,
    integrations: [
      vueIntegration({ app, attachProps: false }),
      globalHandlersIntegration(),
      linkedErrorsIntegration(),
      dedupeIntegration(),
    ],
    maxBreadcrumbs: 0,
    sendClientReports: false,
    beforeSend: scrubEvent,
  })
}
