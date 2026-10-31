import type { ErrorEvent } from '@sentry/vue'
import * as Sentry from '@sentry/vue'
import { createApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FailingComponent } from './failingComponent'
import { installSentry } from '../../src/sentry/install'

type SentEnvelope = [unknown, [{ type: string }, unknown][]]
const sent: SentEnvelope[] = []

// the real `init`, with only the transport replaced so nothing leaves the process
vi.mock('@sentry/vue', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sentry/vue')>()
  return {
    ...actual,
    init: (options: Parameters<typeof actual.init>[0]) =>
      actual.init({
        ...options,
        transport: () => ({
          send: (envelope: SentEnvelope) => {
            sent.push(envelope)
            return Promise.resolve({})
          },
          flush: () => Promise.resolve(true),
        }),
      }),
  }
})

const EmptyComponent = { render: () => null }
const DSN = 'https://publickey@o1.ingest.sentry.io/2'
const PROP_SENTINEL = 'PROP-sentinel-value'
const FRAGMENT_SENTINEL = 'FRAGMENT-sentinel-value'

function pageWith(dsn: string | null) {
  const attributes: Record<string, string> = {
    content: dsn ?? '',
    'data-environment': 'production',
    'data-release': 'abc123',
  }
  return {
    querySelector: () =>
      dsn === null
        ? null
        : ({ getAttribute: (name: string) => attributes[name] ?? null } as Element),
  }
}

function sentEvents(): ErrorEvent[] {
  return sent.flatMap(([, items]) =>
    items.flatMap(([header, payload]) => (header.type === 'event' ? [payload as ErrorEvent] : [])),
  )
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 20))
}

beforeEach(() => {
  sent.length = 0
  vi.stubGlobal('location', { href: `https://host/app#tgWebAppData=${FRAGMENT_SENTINEL}` })
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(async () => {
  await Sentry.close()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('installSentry', () => {
  it('does nothing without the meta tag', () => {
    const app = createApp(EmptyComponent)

    installSentry(app, pageWith(null))

    expect(Sentry.getClient()).toBeUndefined()
    expect(app.config.errorHandler).toBeUndefined()
  })

  it('does nothing for an empty DSN', () => {
    const app = createApp(EmptyComponent)

    installSentry(app, pageWith(''))

    expect(Sentry.getClient()).toBeUndefined()
  })

  it('runs exactly the four integrations, with no tracing and the tags from the page', () => {
    const app = createApp(EmptyComponent)

    installSentry(app, pageWith(DSN))

    const options = Sentry.getClient()?.getOptions()
    expect((options as Record<string, unknown> | undefined)?.defaultIntegrations).toBe(false)
    expect((options?.integrations ?? []).map((integration) => integration.name).sort()).toEqual([
      'Dedupe',
      'GlobalHandlers',
      'LinkedErrors',
      'Vue',
    ])
    for (const unwanted of ['HttpContext', 'Breadcrumbs', 'BrowserSession', 'BrowserApiErrors']) {
      expect(Sentry.getClient()?.getIntegrationByName(unwanted)).toBeUndefined()
    }
    expect(options?.tracesSampleRate).toBeUndefined()
    expect(options?.maxBreadcrumbs).toBe(0)
    expect(options?.environment).toBe('production')
    expect(options?.release).toBe('abc123')
    expect(app._context.mixins).toHaveLength(0)
  })

  it('reports a window error without the page URL, a request section or a breadcrumb', async () => {
    installSentry(createApp(EmptyComponent), pageWith(DSN))

    globalThis.onerror?.('boom', 'https://host/app.js', 1, 1, new Error('boom'))
    await settle()

    const events = sentEvents()
    expect(events).toHaveLength(1)
    expect(events[0]?.request).toBeUndefined()
    expect(events[0]?.user).toBeUndefined()
    expect(JSON.stringify(sent)).not.toContain(FRAGMENT_SENTINEL)
    expect(JSON.stringify(sent)).not.toContain('tgWebAppData')
    expect(new Set(sent.flatMap(([, items]) => items.map(([header]) => header.type)))).toEqual(
      new Set(['event']),
    )
  })

  it('scrubs a bearer string and a token query from the message', async () => {
    installSentry(createApp(EmptyComponent), pageWith(DSN))

    globalThis.onerror?.(
      'failed Bearer BEARER-x at /a?token=QUERY-y',
      'a.js',
      1,
      1,
      new Error('failed Bearer BEARER-x at /a?token=QUERY-y'),
    )
    await settle()

    expect(JSON.stringify(sent)).not.toMatch(/BEARER-x|QUERY-y/)
  })

  async function renderFailingComponent(app: ReturnType<typeof createApp>): Promise<void> {
    await renderToString(app)
    await settle()
  }

  function failingApp() {
    return createApp(FailingComponent, { initData: PROP_SENTINEL })
  }

  it('reports a Vue error once, logs it, and sends no component props', async () => {
    const app = failingApp()
    installSentry(app, pageWith(DSN))

    await renderFailingComponent(app)

    expect(sentEvents()).toHaveLength(1)
    expect(console.error).toHaveBeenCalled()
    expect(JSON.stringify(sent)).not.toContain(PROP_SENTINEL)
    expect(sentEvents()[0]?.contexts?.vue).toBeDefined()
  })

  it('control: with props attached and no scrubber the sentinel does travel', async () => {
    const app = failingApp()
    app.config.errorHandler = (error) => console.error(error)
    Sentry.init({
      dsn: DSN,
      defaultIntegrations: false,
      integrations: [Sentry.vueIntegration({ app, attachProps: true })],
    })

    await renderFailingComponent(app)

    expect(JSON.stringify(sent)).toContain(PROP_SENTINEL)
  })

  it('control: with props attached the real scrubber still removes them', async () => {
    const app = failingApp()
    app.config.errorHandler = (error) => console.error(error)
    const { scrubEvent } = await import('../../src/sentry/scrub')
    Sentry.init({
      dsn: DSN,
      defaultIntegrations: false,
      integrations: [Sentry.vueIntegration({ app, attachProps: true })],
      beforeSend: scrubEvent,
    })

    await renderFailingComponent(app)

    expect(sentEvents()).toHaveLength(1)
    expect(JSON.stringify(sent)).not.toContain(PROP_SENTINEL)
  })
})
