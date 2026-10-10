// The waveform data handed to wavesurfer: the server's peaks, or a flat line when there are none.

// the height is part of the `<style>` text wavesurfer writes, so the CSP hash depends on it
export const WAVEFORM_HEIGHT_PX = 96
export const PEAK_MAX = 255
export const FLAT_LEVEL = 0.05
export const FLAT_VALUES_PER_SECOND = 20

export interface Waveform {
  channel: Float32Array
  flat: boolean
}

export function peakChannelOf(values: number[]): Float32Array {
  return Float32Array.from(values, (value) => Math.min(Math.max(value, 0), PEAK_MAX) / PEAK_MAX)
}

export function flatChannelOf(durationMs: number): Float32Array {
  const length = Math.max(1, Math.ceil((durationMs / 1000) * FLAT_VALUES_PER_SECOND))
  return new Float32Array(length).fill(FLAT_LEVEL)
}

export interface WaveformSource {
  peaks: { values: number[] } | null
  audio_duration_ms: number
}

export function waveformOf(view: WaveformSource): Waveform {
  const values = view.peaks?.values ?? []
  if (values.length === 0) {
    return { channel: flatChannelOf(view.audio_duration_ms), flat: true }
  }
  return { channel: peakChannelOf(values), flat: false }
}
