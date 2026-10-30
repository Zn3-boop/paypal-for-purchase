import { useEffect, useRef, useState } from 'react'
import { API_BASE } from '../api'

// Minimal structural types for the Web Speech API so we don't need the
// DOM/lib.dom types to be globally complete.
type TranscriptResult = { transcript: string }
type SpeechRecognitionResultLike = { isFinal: boolean; 0: TranscriptResult }
type SpeechRecognitionEventLike = {
  resultIndex: number
  results: { length: number; [i: number]: SpeechRecognitionResultLike }
}

/** Turn raw Web Speech error codes into user-friendly guidance strings. */
function friendlyRecognitionError(code: string): string {
  switch (code) {
    case 'network':
      // Chrome desktop streams audio to Google for recognition; offline it dies.
      return 'Voice recognition needs a network connection (read-aloud still works offline).'
    case 'not-allowed':
    case 'service-not-allowed':
      return 'Microphone permission was blocked. Allow the mic for this site in the browser and try again.'
    case 'no-speech':
      return 'No speech detected — try speaking a little closer to the mic.'
    case 'audio-capture':
      return 'No microphone found on this device — type your message instead.'
    case 'aborted':
    case 'canceled':
      return ''
    default:
      return code
  }
}
type SpeechRecognitionLike = {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: SpeechRecognitionEventLike) => void) | null
  onend: (() => void) | null
  onerror: ((e: { error: string }) => void) | null
  start: () => void
  stop: () => void
}

/** Encode mono Float32 PCM into a standard 16-bit 16 kHz WAV Blob (RIFF). */
function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const n = samples.length
  const buffer = new ArrayBuffer(44 + n * 2)
  const view = new DataView(buffer)
  const writeStr = (off: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)) }
  writeStr(0, 'RIFF'); view.setUint32(4, 36 + n * 2, true); writeStr(8, 'WAVE')
  writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  writeStr(36, 'data'); view.setUint32(40, n * 2, true)
  let off = 44
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    off += 2
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

/** Linear-resample to whisper's 16 kHz input rate. */
function resampleTo16k(samples: Float32Array, fromRate: number): Float32Array {
  if (fromRate === 16000) return samples
  const ratio = fromRate / 16000
  const outN = Math.max(1, Math.round(samples.length / ratio))
  const out = new Float32Array(outN)
  for (let i = 0; i < outN; i++) {
    const pos = i * ratio
    const i0 = Math.floor(pos)
    const i1 = Math.min(i0 + 1, samples.length - 1)
    const frac = pos - i0
    out[i] = samples[i0] * (1 - frac) + samples[i1] * frac
  }
  return out
}

/**
 * Speech-to-text for the agent input box.
 *
 * Engine chain (determined once on mount):
 *  1. `whisper` — our FastAPI backend (`/api/v1/stt/*`) runs faster-whisper
 *     tiny.en LOCALLY (int8). No Google dependency — this is the primary path
 *     for networks where webkitSpeechRecognition is dead. Mic audio never
 *     leaves the server machine.
 *  2. `native` — browser Web Speech API fallback where Google IS reachable.
 *  3. `null` — neither available: callers hide the mic button and degrade to
 *     typing (the one-tap scenario buttons still give quick prompts).
 *
 * Recording for the whisper engine: getUserMedia → ScriptProcessor → PCM →
 * downsampled to 16 kHz mono → WAV → upload. Transcribing is async and reported
 * via `transcribing`, so the UI can show progress ("…" instead of a stuck mic).
 */

export type SpeechEngine = 'whisper' | 'native' | null

export function useSpeechInput(onTranscript: (t: string) => void, lang = 'en-US') {
  const [engine, setEngine] = useState<SpeechEngine>(null)
  const [listening, setListening] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [error, setError] = useState('')
  const recRef = useRef<SpeechRecognitionLike | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const recNodeRef = useRef<ScriptProcessorNode | null>(null)
  const chunksRef = useRef<Float32Array[]>([])
  const onTranscriptRef = useRef(onTranscript)

  useEffect(() => {
    onTranscriptRef.current = onTranscript
  }, [onTranscript])

  // ---- engine detection: local whisper backend first, native fallback ----
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const resp = await fetch(`${API_BASE}/stt/status`)
        const data = await resp.json()
        if (!cancelled && data?.enabled) { setEngine('whisper'); return }
      } catch { /* backend unreachable */ }
      if (!cancelled) {
        const w = window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown }
        if (w.SpeechRecognition || w.webkitSpeechRecognition) setEngine('native')
      }
    })()
    return () => { cancelled = true }
  }, [])

  // ---- native recognition instance (only when engine === 'native') ----
  useEffect(() => {
    if (engine !== 'native' || typeof window === 'undefined') return
    const w = window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionLike
      webkitSpeechRecognition?: new () => SpeechRecognitionLike
    }
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition
    if (!Ctor) return

    const rec = new Ctor()
    rec.lang = lang
    rec.interimResults = true
    rec.continuous = true
    let final = ''
    let last = ''
    rec.onresult = (e) => {
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const tr = e.results[i]?.[0]?.transcript ?? ''
        if (e.results[i].isFinal) final += tr
        else interim += tr
      }
      last = final + interim
      onTranscriptRef.current(last)
    }
    rec.onend = () => setListening(false)
    rec.onerror = (e) => { setError(friendlyRecognitionError(e.error)); setListening(false) }
    recRef.current = rec

    return () => {
      rec.onend = null
      rec.onerror = null
      try { rec.stop() } catch { /* already stopped */ }
    }
  }, [engine, lang])
// ---- whisper engine: record until stop, then transcribe locally ----
  const startWhisper = async () => {
    setError('')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext()
      if (ctx.state === 'suspended') await ctx.resume()
      const src = ctx.createMediaStreamSource(stream)
      const rec = ctx.createScriptProcessor(4096, 1, 1)
      const chunks: Float32Array[] = []
      rec.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)))
      src.connect(rec)
      rec.connect(ctx.destination) // keep the graph alive / Safari requirement
      streamRef.current = stream
      audioCtxRef.current = ctx
      recNodeRef.current = rec
      chunksRef.current = chunks
      setListening(true)
    } catch {
      setError('Microphone unavailable — allow the permission prompt or plug in a mic, then type instead.')
    }
  }

  const stopWhisper = async () => {
    setListening(false)
    setTranscribing(true)
    const ctx = audioCtxRef.current
    const chunks = chunksRef.current
    try { recNodeRef.current?.disconnect() } catch { /* noop */ }
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
    try {
      if (!chunks.length || !ctx) {
        setError('No audio captured — tap the mic again and speak for at least ~1 second.')
        return
      }
      const total = chunks.reduce((s, c) => s + c.length, 0)
      const all = new Float32Array(total)
      let off = 0
      for (const c of chunks) { all.set(c, off); off += c.length }
      const mono = resampleTo16k(all, ctx.sampleRate)
      const form = new FormData()
      form.append('upload', encodeWav(mono, 16000), 'speech.wav')
      const resp = await fetch(`${API_BASE}/stt/transcribe`, { method: 'POST', body: form })
      if (!resp.ok) {
        const body = await resp.json().catch(() => null)
        throw new Error((body as { detail?: string } | null)?.detail || `Transcription failed (${resp.status})`)
      }
      const data = await resp.json()
      if (data?.text) onTranscriptRef.current(data.text.trim())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Transcription failed — type your message instead.')
    } finally {
      setTranscribing(false)
      if (ctx) { ctx.close().catch(() => {}) }
      audioCtxRef.current = null
      streamRef.current = null
      recNodeRef.current = null
      chunksRef.current = []
    }
  }

  const start = () => {
    setError('')
    if (engine === 'whisper') { void startWhisper(); return }
    if (engine === 'native' && recRef.current) {
      setListening(true)
      try { recRef.current.start() } catch { /* already started */ }
    }
  }
  const stop = () => {
    if (engine === 'whisper') { void stopWhisper(); return }
    recRef.current?.stop()
  }
  const toggle = () => (listening ? stop() : start())

  // unmount cleanup: never leave the mic hot
  useEffect(() => () => {
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
    recNodeRef.current?.disconnect()
    if (audioCtxRef.current) audioCtxRef.current.close().catch(() => {})
  }, [])

  return { supported: engine !== null, engine, listening, transcribing, error, start, stop, toggle }
}
