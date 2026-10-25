"use strict";
// Live voice on top of the same assistant as chat.
//
// Microphone frames go through a small voice-activity detector. When the customer stops talking, the
// utterance is sent as WAV to /api/voice/turn, which transcribes it and streams the same events as chat.
// Replies are spoken sentence by sentence as they stream in, and a short acknowledgement is spoken the
// moment a lookup starts.
//
// Interruptions are two-step so noise or the assistant's own echo can't cancel a turn:
//   1. The moment sound is detected over the assistant, its voice is ducked (turned down), nothing more.
//   2. The utterance goes to the server, which transcribes it and decides. Real words take over: the old
//      turn is cancelled and its speech stopped. Silence, noise or echo is dropped and the volume comes back.

const VAD = {
  calibrateMs: 500,    // listen to the room first to learn its noise level
  factor: 2.5,         // speech must be this many times louder than the room
  minLevel: 0.005,     // ... and at least this loud
  playbackLevel: 0.02, // minimum while the assistant is talking, so its echo is less likely to count
  startMs: 120,        // continuous speech needed to count as the customer talking
  endMs: 800,          // silence that ends an utterance
  minUtteranceMs: 300,
  minVoicedMs: 250,    // ... of which at least this much must be clearly above the threshold
  maxUtteranceMs: 15000,
  prerollMs: 400,      // audio kept from just before speech was detected
  manualMs: 15000,     // audio kept for the Send button when detection never fired
  idlePauseMs: 12000,  // mic turns off after this long with no speech and nothing in progress
};

const voice = {
  active: false, ctx: null, stream: null, sink: null, out: null, frameCount: 0, peak: 0,
  calibrating: true, calib: [], floor: 0.003, threshold: VAD.minLevel,
  inSpeech: false, speechMs: 0, silenceMs: 0, voicedMs: 0, frames: [], preroll: [], manual: [],
  queue: [], playing: null, epoch: 0, ttsAbort: null, heard: [], ducked: false,
  seq: 0, currentSeq: 0, busy: false, pendingSends: 0, turnId: null, turnEnded: false,
  textStarted: false, sentenceBuf: "", fillerSaid: false, slowTimer: null,
  speechEndAt: 0, firstAudioMs: null, firstAnswerAudioMs: null, metricsSent: false, meterAt: 0,
  ready: {},  // pre-generated audio for the short phrases spoken while lookups run
  ttsMode: null,  // "server" or "browser", fixed by the first sentence so one conversation keeps one voice
  lastActivity: 0, paused: false,
};

function touch() { voice.lastActivity = performance.now(); }

const ACK = "Let me check.";
const STILL = "Still checking, one moment.";
state.voice = voice;

// ---------------------------------------------------------------------------
// Start / stop
// ---------------------------------------------------------------------------
async function startVoice() {
  if (voice.active || !state.session) return;
  if (!state.config.voice?.stt) {
    flashVoiceNote("Voice needs GROQ_API_KEY in .env for speech-to-text.");
    return;
  }
  // Create the audio engine inside the click, before the permission prompt, so the browser lets it run.
  const ctx = new AudioContext();
  ctx.resume().catch(() => {});
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch {
    ctx.close();
    flashVoiceNote("Microphone access was blocked. Allow it in the browser's site settings and try again.");
    return;
  }
  await ctx.resume().catch(() => {});
  await ctx.audioWorklet.addModule("/static/capture-worklet.js");
  const source = ctx.createMediaStreamSource(voice.stream);
  const node = new AudioWorkletNode(ctx, "capture");
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(node).connect(mute).connect(ctx.destination);  // keeps the worklet running, silently
  node.port.onmessage = (e) => onFrame(e.data);
  const out = ctx.createGain();  // the assistant's voice goes through here so it can be ducked
  out.connect(ctx.destination);
  Object.assign(voice, {
    ctx, sink: node, out, active: true, frameCount: 0, peak: 0, calibrating: true, calib: [], floor: 0.003,
    inSpeech: false, speechMs: 0, silenceMs: 0, frames: [], preroll: [], manual: [], ducked: false,
  });
  voice.ttsAbort = new AbortController();
  voice.ttsMode = state.config.voice?.tts === "server" ? "server" : "browser";
  // Generate the acknowledgement phrases now so they can play the instant a lookup starts.
  voice.ready = { [ACK]: fetchSpeech(ACK, false), [STILL]: fetchSpeech(STILL, false) };
  voice.paused = false;
  touch();
  $("#voicebar").hidden = false;
  $("#vresume").hidden = true;
  $("#vsend").hidden = false;
  $("#mic").classList.add("on");
  $("#mic").title = "End voice";
  $("#suggestions").hidden = true;
  setVoiceState("listening");
  setTimeout(() => {
    if (!voice.active) return;
    if (voice.ctx.state !== "running") {
      voice.ctx.resume();
      flashVoiceNote("The browser paused audio. Click anywhere on the page, then speak.");
    } else if (voice.frameCount === 0) {
      flashVoiceNote("No sound is coming from the microphone. Check the input device in your system settings.");
    }
  }, 1500);
  setTimeout(() => {
    if (voice.active && voice.frameCount > 0 && voice.peak === 0) {
      flashVoiceNote("The microphone is sending silence. On a Mac, allow your browser under System Settings > Privacy & Security > Microphone, then reload.");
    }
  }, 3000);
}

// End voice. Anything said and not yet sent goes first, and the answer arrives as text.
function stopVoice() {
  if (voice.paused && !voice.active) {  // ending from the paused state
    voice.paused = false;
    $("#voicebar").hidden = true;
    return;
  }
  if (!voice.active) return;
  if (voice.inSpeech) sendNow();
  stopPlayback();
  voice.stream?.getTracks().forEach((t) => t.stop());
  voice.ctx?.close();
  Object.assign(voice, { active: false, ctx: null, stream: null, sink: null, out: null, inSpeech: false,
    frames: [], preroll: [], manual: [] });
  $("#voicebar").hidden = true;
  $("#mic").classList.remove("on");
  $("#mic").title = "Talk";
}

// Nothing said for a while: turn the microphone off, keep the conversation, offer to resume.
function pauseVoice() {
  stopVoice();
  voice.paused = true;
  $("#voicebar").hidden = false;
  $("#vresume").hidden = false;
  $("#vsend").hidden = true;
  setVoiceState("paused");
}

// Send button: end the utterance now. Works even if automatic detection never heard the customer start.
function sendNow() {
  if (!voice.active) return;
  const frames = voice.inSpeech ? voice.frames : trimSilence(voice.manual);
  Object.assign(voice, { inSpeech: false, frames: [], speechMs: 0, silenceMs: 0, manual: [] });
  const ms = frames.reduce((n, f) => n + f.length, 0) / voice.ctx.sampleRate * 1000;
  if (!frames.length || ms < VAD.minUtteranceMs) {
    flashVoiceNote("I didn't hear anything. Watch the level bars turn green while you speak.");
    return;
  }
  utteranceReady(frames);
}

function trimSilence(frames) {
  const loud = (f) => rmsOf(f) > Math.max(voice.floor * 1.8, VAD.minLevel * 0.6);
  const first = frames.findIndex(loud);
  if (first < 0) return [];
  let last = frames.length - 1;
  while (last > first && !loud(frames[last])) last--;
  const pad = Math.round(VAD.prerollMs / 20);
  return frames.slice(Math.max(0, first - pad), Math.min(frames.length, last + pad));
}

function rmsOf(frame) {
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  return Math.sqrt(sum / frame.length);
}

// ---------------------------------------------------------------------------
// Voice activity detection
// ---------------------------------------------------------------------------
function onFrame(frame) {
  voice.frameCount += 1;
  const ms = (frame.length / voice.ctx.sampleRate) * 1000;
  const rms = rmsOf(frame);
  voice.peak = Math.max(voice.peak, rms);
  voice.manual.push(frame);
  while (voice.manual.length * ms > VAD.manualMs) voice.manual.shift();

  if (voice.calibrating) {
    voice.calib.push(rms);
    if (voice.calib.length * ms >= VAD.calibrateMs) {
      const sorted = [...voice.calib].sort((a, b) => a - b);
      voice.floor = Math.min(Math.max(sorted[Math.floor(sorted.length * 0.3)], 0.0005), 0.02);
      voice.calibrating = false;
    }
    drawMeter(rms, false);
    return;
  }

  if (voice.inSpeech || voice.playing || voice.busy || voice.pendingSends) {
    touch();
  } else if (performance.now() - voice.lastActivity > VAD.idlePauseMs) {
    pauseVoice();
    return;
  }

  let threshold = Math.max(voice.floor * VAD.factor, VAD.minLevel);
  if (voice.playing && !voice.ducked) threshold = Math.max(threshold * 1.6, VAD.playbackLevel);
  voice.threshold = threshold;
  const speech = rms > threshold;
  drawMeter(rms, speech);

  if (!voice.inSpeech) {
    voice.preroll.push(frame);
    while (voice.preroll.length * ms > VAD.prerollMs) voice.preroll.shift();
    if (speech) {
      voice.speechMs += ms;
      if (voice.speechMs >= VAD.startMs) {
        voice.inSpeech = true;
        voice.silenceMs = 0;
        voice.voicedMs = voice.speechMs;
        voice.frames = voice.preroll.slice();
        voice.preroll = [];
        onSpeechStart();
      }
    } else {
      voice.speechMs = 0;
      // Learn the room only from clearly quiet frames, so quiet speech can't raise the bar.
      if (rms < threshold * 0.5) voice.floor = Math.min(voice.floor * 0.98 + rms * 0.02, 0.02);
    }
    return;
  }

  voice.frames.push(frame);
  voice.silenceMs = speech ? 0 : voice.silenceMs + ms;
  if (speech) voice.voicedMs += ms;
  const length = voice.frames.length * ms;
  if (voice.silenceMs >= VAD.endMs || length >= VAD.maxUtteranceMs) {
    const frames = voice.frames;
    const spoken = length - voice.silenceMs;
    const voiced = voice.voicedMs;
    Object.assign(voice, { inSpeech: false, frames: [], speechMs: 0, silenceMs: 0, voicedMs: 0, manual: [] });
    if (spoken < VAD.minUtteranceMs || voiced < VAD.minVoicedMs) {
      unduck();  // a cough or a click: carry on
      restoreState();
      return;
    }
    utteranceReady(frames);
  }
}

function onSpeechStart() {
  if (voice.playing) duck();
  setVoiceState("hearing");
}

function duck() {
  if (voice.ducked) return;
  voice.ducked = true;
  voice.out?.gain.setTargetAtTime(0.12, voice.ctx.currentTime, 0.03);
  if (voice.playing?.browser) speechSynthesis.pause();
}

function unduck() {
  if (!voice.ducked) return;
  voice.ducked = false;
  voice.out?.gain.setTargetAtTime(1, voice.ctx.currentTime, 0.05);
  if (voice.playing?.browser) speechSynthesis.resume();
}

function restoreState() {
  setVoiceState(voice.playing ? "speaking" : voice.busy ? "thinking" : voice.pendingSends ? "transcribing" : "listening");
}

// ---------------------------------------------------------------------------
// Turns
// ---------------------------------------------------------------------------
function utteranceReady(frames) {
  voice.speechEndAt = performance.now();
  voice.lastUtteranceMs = frames.reduce((n, f) => n + f.length, 0) / voice.ctx.sampleRate * 1000;
  const interrupting = voice.busy || !!voice.playing;
  const heard = [...voice.heard, ...(voice.playing ? [`${voice.playing.text} [cut off]`] : [])].join(" ").slice(-500);
  sendVoiceTurn(encodeWav(frames, voice.ctx.sampleRate), interrupting, heard);
}

async function sendVoiceTurn(wav, interrupting, heard) {
  const seq = ++voice.seq;
  voice.pendingSends += 1;
  if (!voice.busy && !voice.playing) setVoiceState("transcribing");
  const view = await streamTurn({
    url: `/api/voice/turn?interrupted=${interrupting ? "true" : "false"}`,
    body: wav,
    headers: { "Content-Type": "audio/wav", "X-Heard-Text": encodeURIComponent(heard) },
    hook: (v, ev) => voiceHook(seq, v, ev),
    lockInput: false,
  });
  if (view.accepted === undefined) {  // the request failed before the server decided
    voice.pendingSends = Math.max(0, voice.pendingSends - 1);
    unduck();
  }
  if (seq === voice.currentSeq) {
    clearTimeout(voice.slowTimer);
    flushSentences(true);
    voice.busy = false;
  }
  if (!voice.playing && !voice.queue.length && voice.active && !voice.inSpeech) restoreState();
}

// The server accepted this utterance as a real request: it becomes the conversation's current turn.
function takeOver(seq) {
  stopPlayback();  // the interrupted answer stops here
  Object.assign(voice, {
    currentSeq: seq, busy: true, turnId: null, turnEnded: false, textStarted: false, sentenceBuf: "",
    fillerSaid: false, heard: [], firstAudioMs: null, firstAnswerAudioMs: null, metricsSent: false,
  });
}

function voiceHook(seq, view, ev) {
  if (ev.type === "transcript" || ev.type === "no_speech") {
    voice.pendingSends = Math.max(0, voice.pendingSends - 1);
    view.accepted = ev.type === "transcript";
  }
  if (ev.type === "no_speech") {
    unduck();
    restoreState();
    // Only worth a message when it sounded like a real attempt to say something.
    if (!voice.busy && !voice.playing && voice.lastUtteranceMs > 1200) flashVoiceNote("Didn't catch that. Try again.");
    return;
  }
  if (ev.type === "transcript") {
    takeOver(seq);
    addUser(ev.text, true);
    setVoiceState("thinking");
    return;
  }
  if (seq !== voice.currentSeq) return;  // a superseded turn: still shown on screen, no longer spoken
  switch (ev.type) {
    case "turn_start":
      voice.turnId = ev.turn_id;
      break;
    case "tool_pending":
      if (!voice.fillerSaid && !voice.textStarted) {
        voice.fillerSaid = true;
        speak(ACK, "filler");
        voice.slowTimer = setTimeout(() => { if (!voice.textStarted) speak(STILL, "filler"); }, 3500);
      }
      break;
    case "text_delta":
      voice.textStarted = true;
      clearTimeout(voice.slowTimer);
      voice.sentenceBuf += ev.text;
      flushSentences(false);
      break;
    case "turn_end":
      voice.turnEnded = true;
      voice.turnId = ev.metrics.turn_id;
      flushSentences(true);
      reportVoiceMetrics();
      break;
  }
}

// Speak complete sentences as soon as they exist; the last fragment waits for the end of the turn.
function flushSentences(final) {
  let buf = voice.sentenceBuf;
  const out = [];
  const boundary = /^([\s\S]*?(?:[.!?](?=\s)|\n))\s*/;
  for (;;) {
    const m = buf.match(boundary);
    if (!m) break;
    const sentence = m[1].trim();
    if (sentence.length < 12 && buf.length > m[0].length) {
      // too short to speak alone ("Yes." "Sure."): keep it with the next sentence
      const next = buf.slice(m[0].length).match(boundary);
      if (!next) break;
      out.push(`${sentence} ${next[1].trim()}`);
      buf = buf.slice(m[0].length + next[0].length);
      continue;
    }
    if (sentence) out.push(sentence);
    buf = buf.slice(m[0].length);
  }
  if (final && buf.trim()) {
    out.push(buf.trim());
    buf = "";
  }
  voice.sentenceBuf = buf;
  out.forEach((sentence) => speak(sentence));
}

// ---------------------------------------------------------------------------
// Speech output: fetch audio for each sentence straight away, play strictly in order
// ---------------------------------------------------------------------------
function speak(text, kind = "answer") {
  if (!voice.active || !text.trim()) return;
  const item = { text, kind, epoch: voice.epoch, audio: voice.ready[text] || fetchSpeech(text) };
  voice.queue.push(item);
  if (!voice.playing && voice.queue.length === 1) playNext();
}

async function fetchSpeech(text, cancellable = true) {
  try {
    const res = await fetch("/api/voice/tts", {
      method: "POST", signal: cancellable ? voice.ttsAbort.signal : undefined,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.session.token}` },
      body: JSON.stringify({ text }),
    });
    if (res.status === 204) return { skip: true };
    if (res.status === 503) return voiceFallback((await res.json()).speak);
    if (!res.ok) return voiceFallback(text);
    const buffer = await voice.ctx.decodeAudioData(await res.arrayBuffer());
    return { buffer };
  } catch (e) {
    return e.name === "AbortError" ? { skip: true } : voiceFallback(text);
  }
}

// Never mix voices mid-conversation: in server mode a failed sentence is shown but not spoken.
function voiceFallback(text) {
  if (voice.ttsMode === "server") {
    flashVoiceNote("Voice output hiccuped; the rest of the answer is on screen.");
    return { skip: true };
  }
  return { fallback: text };
}

async function playNext() {
  const item = voice.queue[0];
  if (!item) {
    voice.playing = null;
    if (voice.active && !voice.inSpeech) restoreState();
    return;
  }
  voice.playing = { text: item.text, stop: () => {} };
  const result = await item.audio;
  if (item.epoch !== voice.epoch) return;  // interrupted while the audio was loading
  voice.queue.shift();
  const done = () => {
    if (item.epoch !== voice.epoch) return;
    voice.heard.push(item.text);
    playNext();
  };
  if (result.skip) return done();
  markFirstAudio(item.kind);
  if (!voice.inSpeech) setVoiceState("speaking");
  if (result.buffer) {
    const src = voice.ctx.createBufferSource();
    src.buffer = result.buffer;
    src.connect(voice.out);
    src.onended = done;
    voice.playing = { text: item.text, stop: () => { src.onended = null; try { src.stop(); } catch { /* already stopped */ } } };
    src.start();
  } else {
    const utter = new SpeechSynthesisUtterance(result.fallback);
    utter.voice = browserVoice();
    utter.rate = 1.05;
    utter.onend = done;
    utter.onerror = done;
    voice.playing = { text: item.text, browser: true,
      stop: () => { utter.onend = null; utter.onerror = null; speechSynthesis.cancel(); } };
    speechSynthesis.speak(utter);
    if (voice.ducked) speechSynthesis.pause();
  }
}

function stopPlayback() {
  voice.epoch += 1;
  voice.playing?.stop();
  voice.playing = null;
  voice.queue = [];
  voice.ttsAbort?.abort();
  voice.ttsAbort = new AbortController();
  clearTimeout(voice.slowTimer);
  if (voice.out) voice.out.gain.setTargetAtTime(1, voice.ctx.currentTime, 0.01);
  voice.ducked = false;
}

let cachedBrowserVoice;
function browserVoice() {
  if (cachedBrowserVoice !== undefined) return cachedBrowserVoice;
  const voices = speechSynthesis.getVoices();
  cachedBrowserVoice = voices.find((v) => v.lang === "en-IN") || voices.find((v) => v.lang === "en-GB")
    || voices.find((v) => v.lang?.startsWith("en")) || null;
  return cachedBrowserVoice;
}
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = () => { cachedBrowserVoice = undefined; };

// ---------------------------------------------------------------------------
// Latency: from the moment the customer stopped talking to the first sound of the reply
// ---------------------------------------------------------------------------
function markFirstAudio(kind) {
  if (!voice.speechEndAt) return;
  const ms = Math.round(performance.now() - voice.speechEndAt);
  if (voice.firstAudioMs === null) voice.firstAudioMs = ms;
  if (kind === "answer" && voice.firstAnswerAudioMs === null) {
    voice.firstAnswerAudioMs = ms;
    showVoiceLatency();
    reportVoiceMetrics();
  }
}

function showVoiceLatency() {
  const last = [...document.querySelectorAll(".msg.assistant .meta")].pop();
  if (!last) return;
  const ack = voice.firstAudioMs < voice.firstAnswerAudioMs ? `acknowledged ${secs(voice.firstAudioMs)} · ` : "";
  last.append(h(`<span class="lat" title="Measured from the end of your speech (after the ${VAD.endMs} ms pause that marks it)">🔊 ${ack}answer audio ${secs(voice.firstAnswerAudioMs)}</span>`));
}

function reportVoiceMetrics() {
  if (voice.metricsSent || voice.firstAnswerAudioMs === null || !voice.turnEnded || !voice.turnId) return;
  voice.metricsSent = true;
  api("/api/voice/metrics", { method: "POST", body: JSON.stringify({
    turn_id: voice.turnId, first_audio_ms: voice.firstAudioMs, first_answer_audio_ms: voice.firstAnswerAudioMs,
  }) }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Encoding: 16 kHz mono 16-bit WAV, which every speech-to-text API accepts
// ---------------------------------------------------------------------------
function encodeWav(frames, inputRate) {
  const total = frames.reduce((n, f) => n + f.length, 0);
  const input = new Float32Array(total);
  let offset = 0;
  for (const f of frames) { input.set(f, offset); offset += f.length; }
  const ratio = inputRate / 16000;
  const length = Math.floor(total / ratio);
  const pcm = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio), end = Math.min(Math.floor((i + 1) * ratio), total);
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    const v = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); view.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); str(36, "data"); view.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  return new Blob([buffer], { type: "audio/wav" });
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------
const VOICE_STATES = {
  listening: "Listening",
  hearing: "Hearing you",
  transcribing: "Transcribing",
  thinking: "Thinking",
  speaking: "Speaking · talk to interrupt",
  paused: "Paused · mic off after 12 s of quiet",
};

function setVoiceState(s) {
  const bar = $("#voicebar");
  bar.dataset.state = s;
  $("#vstate").textContent = VOICE_STATES[s] || s;
}

function drawMeter(rms, speech) {
  const now = performance.now();
  if (now - voice.meterAt < 60) return;
  voice.meterAt = now;
  // Scaled so the threshold sits about halfway up: you can see when your voice crosses it.
  const level = Math.min(1, rms / (voice.threshold * 2));
  const meter = $("#vmeter");
  meter.classList.toggle("speech", !!speech);
  meter.title = `mic level ${rms.toFixed(4)} · speech above ${voice.threshold.toFixed(4)}`;
  meter.querySelectorAll("i").forEach((bar, i) => {
    bar.style.transform = `scaleY(${Math.max(0.12, Math.min(1, level * (1.4 - Math.abs(i - 2) * 0.25)))})`;
  });
}

function flashVoiceNote(text) {
  const note = $("#vnote");
  note.textContent = text;
  note.hidden = false;
  clearTimeout(flashVoiceNote.timer);
  flashVoiceNote.timer = setTimeout(() => { note.hidden = true; }, 7000);
}

$("#mic").onclick = () => (voice.active ? stopVoice() : startVoice());
$("#vstop").onclick = stopVoice;
$("#vsend").onclick = sendNow;
$("#vresume").onclick = startVoice;
