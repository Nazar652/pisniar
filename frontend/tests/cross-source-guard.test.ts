import { describe, expect, it, vi } from 'vitest'
import { DISCARD_QUESTION, confirmDiscardOthers, createLeaveGuard } from '../src/app/leaveGuard'
import { useCancelPreparation } from '../src/fragment/useCancelPreparation'
import { useFragmentEditor } from '../src/fragment/useFragmentEditor'
import { useLyricsEditor } from '../src/lyrics/useLyricsEditor'
import {
  fakeServer,
  jsonResponse,
  makeFragmentView,
  makeLyricsView,
  type FakeServer,
} from './support'

const FRAGMENT_GET = 'GET /api/posts/7/fragment'
const FRAGMENT_POST = 'POST /api/posts/7/fragment'
const LYRICS_GET = 'GET /api/posts/7/lyrics'
const CANCEL = 'POST /api/posts/7/fragment/cancel'
const STARTED = { post_id: 7, status_label: 'Рендериться', start_ms: 0, end_ms: 45_000 }

function server(): FakeServer {
  return fakeServer({
    [FRAGMENT_GET]: () => jsonResponse(makeFragmentView()),
    [FRAGMENT_POST]: () => jsonResponse(STARTED),
    [LYRICS_GET]: () => jsonResponse(makeLyricsView()),
    'PUT /api/posts/7/lyrics': () => jsonResponse(makeLyricsView({ lyrics_version: 'v2' })),
    [CANCEL]: () => jsonResponse({ post_id: 7, status_label: 'Чернетка готова' }),
  })
}

describe('othersDirty', () => {
  it('ignores the asking source and sees any other', () => {
    const guard = createLeaveGuard()
    expect(guard.othersDirty('lyrics')).toBe(false)

    guard.setDirty('lyrics', true)
    expect(guard.othersDirty('lyrics')).toBe(false)
    expect(guard.othersDirty('fragment')).toBe(true)

    guard.setDirty('lyrics', false)
    expect(guard.othersDirty('fragment')).toBe(false)
  })

  it('asks the discard question only when another source is dirty, and never without a guard', async () => {
    const guard = createLeaveGuard()
    const confirm = vi.fn(async () => true)

    expect(await confirmDiscardOthers(guard, 'fragment', confirm)).toBe(true)
    expect(await confirmDiscardOthers(null, 'fragment', confirm)).toBe(true)
    expect(confirm).not.toHaveBeenCalled()

    guard.setDirty('lyrics', true)
    expect(await confirmDiscardOthers(guard, 'fragment', confirm)).toBe(true)
    expect(confirm).toHaveBeenCalledExactlyOnceWith(DISCARD_QUESTION)
  })
})

describe('a fragment submit over unsaved lyrics', () => {
  async function ready(answer: boolean) {
    const fake = server()
    const guard = createLeaveGuard()
    guard.setDirty('lyrics', true)
    const confirm = vi.fn(async () => answer)
    const editor = useFragmentEditor(fake.client, 7, undefined, () =>
      confirmDiscardOthers(guard, 'fragment', confirm),
    )
    await editor.load()
    return { fake, confirm, editor }
  }

  it('sends nothing when the admin declines', async () => {
    const { fake, confirm, editor } = await ready(false)

    await editor.submit()

    expect(confirm).toHaveBeenCalledExactlyOnceWith(DISCARD_QUESTION)
    expect(fake.callsTo(FRAGMENT_POST)).toBe(0)
    expect(editor.status.value).toBe('ready')
  })

  it('submits when the admin accepts', async () => {
    const { fake, editor } = await ready(true)

    await editor.submit()

    expect(fake.callsTo(FRAGMENT_POST)).toBe(1)
    expect(editor.status.value).toBe('submitted')
  })
})

describe('a cancel over unsaved lyrics', () => {
  function cancellation(answer: boolean) {
    const fake = server()
    const guard = createLeaveGuard()
    guard.setDirty('lyrics', true)
    const confirm = vi.fn(async () => answer)
    const finished = vi.fn()
    const cancel = useCancelPreparation(
      fake.client,
      7,
      () => 'stamp',
      finished,
      () => confirmDiscardOthers(guard, 'fragment', confirm),
    )
    return { fake, confirm, finished, cancel }
  }

  it('sends nothing when the admin declines', async () => {
    const { fake, confirm, finished, cancel } = cancellation(false)

    await cancel.cancel()

    expect(confirm).toHaveBeenCalledExactlyOnceWith(DISCARD_QUESTION)
    expect(fake.callsTo(CANCEL)).toBe(0)
    expect(finished).not.toHaveBeenCalled()
    expect(cancel.busy.value).toBe(false)
  })

  it('cancels when the admin accepts', async () => {
    const { fake, finished, cancel } = cancellation(true)

    await cancel.cancel()

    expect(fake.callsTo(CANCEL)).toBe(1)
    expect(finished).toHaveBeenCalledExactlyOnceWith(true)
  })
})

describe('lyrics save-and-render over an unsubmitted fragment', () => {
  async function ready(answer: boolean) {
    const fake = server()
    const guard = createLeaveGuard()
    guard.setDirty('fragment', true)
    const confirm = vi.fn(async () => answer)
    const editor = useLyricsEditor(fake.client, 7, {
      confirm: async () => true,
      beforeRender: () => confirmDiscardOthers(guard, 'lyrics', confirm),
    })
    await editor.load()
    return { fake, confirm, editor }
  }

  it('renders nothing when the admin declines', async () => {
    const { fake, confirm, editor } = await ready(false)

    expect(await editor.save(true)).toBe(false)

    expect(confirm).toHaveBeenCalledExactlyOnceWith(DISCARD_QUESTION)
    expect(fake.callsTo(FRAGMENT_POST)).toBe(0)
    expect(editor.status.value).toBe('ready')
  })

  it('renders when the admin accepts', async () => {
    const { fake, editor } = await ready(true)

    expect(await editor.save(true)).toBe(true)

    expect(fake.callsTo(FRAGMENT_POST)).toBe(1)
  })

  it('does not ask for a plain save, which queues no render', async () => {
    const { confirm, editor } = await ready(false)
    editor.addLine(0)
    editor.edit(0, 'Новий')

    expect(await editor.save()).toBe(true)

    expect(confirm).not.toHaveBeenCalled()
  })
})
