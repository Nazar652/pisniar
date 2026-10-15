import { describe, expect, it } from 'vitest'
import { createMainButtonGate } from '../src/app/mainButtonGate'

describe('the main button gate', () => {
  it('is open until a screen asks for the button to be hidden', () => {
    const gate = createMainButtonGate()

    expect(gate.hidden.value).toBe(false)
    gate.hide()
    expect(gate.hidden.value).toBe(true)
  })

  it('gives the button back when the screen lets go', () => {
    const gate = createMainButtonGate()
    const release = gate.hide()

    release()

    expect(gate.hidden.value).toBe(false)
  })

  it('counts a letting go once, so a second call cannot undo another hold', () => {
    const gate = createMainButtonGate()
    const first = gate.hide()
    gate.hide()

    first()
    first()

    expect(gate.hidden.value).toBe(true)
  })

  it('stays hidden until every hold is released', () => {
    const gate = createMainButtonGate()
    const first = gate.hide()
    const second = gate.hide()

    first()
    expect(gate.hidden.value).toBe(true)
    second()
    expect(gate.hidden.value).toBe(false)
  })
})
