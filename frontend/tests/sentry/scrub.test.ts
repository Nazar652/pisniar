import { describe, expect, it } from 'vitest'
import { FILTERED, scrubEvent, scrubText } from '../../src/sentry/scrub'

describe('scrubText', () => {
  it.each([
    'signed eyJ1aWQiOjF9.c2ln-_Zm9v end',
    'Authorization: Bearer abc.def-ghi',
    'GET /x?token=SECRET&y=1',
    'GET /x?y=1&token=SECRET',
    'https://host/#tgWebAppData=query_id%3DAAH&tgWebAppVersion=9',
    'fields hash=SECRET and auth_date=17',
    'signature=SECRET chat_instance=1 chat_type=sender start_param=x user=%7B%7D',
  ])('removes the credential shape in %j', (text) => {
    const scrubbed = scrubText(text)

    expect(scrubbed).toContain(FILTERED)
    expect(scrubbed).not.toMatch(/SECRET|eyJ|abc\.def|tgWebAppData|query_id/)
  })

  it('leaves an innocent message alone', () => {
    expect(scrubText('Cannot read properties of undefined')).toBe(
      'Cannot read properties of undefined',
    )
  })
})

describe('scrubEvent', () => {
  it('scrubs every string at any depth and keeps the shape', () => {
    const event = {
      message: 'Bearer x1',
      extra: { list: ['ok', 'a?token=b'], count: 3, none: null },
    }

    expect(scrubEvent(event)).toEqual({
      message: FILTERED,
      extra: { list: ['ok', `a${FILTERED}`], count: 3, none: null },
    })
  })

  it('drops propsData by key at any depth', () => {
    const event = {
      contexts: { vue: { propsData: { initData: 'plain' }, deep: [{ propsData: 1 }] } },
    }

    expect(scrubEvent(event)).toEqual({
      contexts: { vue: { propsData: FILTERED, deep: [{ propsData: FILTERED }] } },
    })
  })

  it('scrubs a credential used as an object key', () => {
    const event = { extra: { 'Bearer x1': 'kept', nested: { 'a?token=SECRET': 1 } } }

    const scrubbed = JSON.stringify(scrubEvent(event))

    expect(scrubbed).not.toMatch(/x1|SECRET/)
    expect(scrubbed).toContain('kept')
  })
})
