// What the editor is playing: nothing, the whole track, or the chosen fragment up to its end.

export type PlayMode = 'idle' | 'track' | 'fragment'

// the stop is decided here on every time update, not left to wavesurfer's private stop position
export function shouldStopFragment(mode: PlayMode, endMs: number, currentSeconds: number): boolean {
  return mode === 'fragment' && currentSeconds * 1000 >= endMs
}
