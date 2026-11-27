/* Speech-to-text for the CA drawer's microphone (task #727): the browser streams its audio straight to the speech provider
 * (Amazon Transcribe streaming) on a short-lived signed address from GET /ca/speech/url; the provider's credentials never reach
 * the browser. The behaviour is the voice rule of the other assistant surfaces: the mic NEVER TIMES OUT while the page is visible and speech is
 * coming in, NEVER SENDS by itself, and stays lit (the accent colour) until Enter or a second tap. The transcript goes into the
 * textarea (typed text before it is kept as the prefix and never clobbered). A socket that closes for any reason while the mic is on
 * is replaced on a fresh address and the audio spoken meanwhile is replayed. Safety stops, as the assistant has: the page going
 * hidden, and 10 minutes with no final transcript; each keeps the text and says so.
 *
 * crewbossSpeech(input, micButton, opts) -> {recording(), stop(), toggle()}
 *   opts.deviceId  string | () => string   the chosen input (exact); a vanished one falls back to the default (opts.onDeviceGone)
 *   opts.onState('idle'|'starting'|'recording')   opts.onMessage(text)   opts.onLevel not needed: crewbossVoice.vad(ctx, src) is exported
 *   opts.stream(stream, ctx)  called once the microphone is open, so the page can drive a level meter from the same stream
 */
(function (root) {
  'use strict';

  /* ── AWS event-stream framing ─────────────────────────────────────────────────────────────────────────── */
  var CRC_TABLE = (function () {
    var t = [], c, n, k;
    for (n = 0; n < 256; n++) { c = n; for (k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1); t[n] = c >>> 0; }
    return t;
  })();
  function crc32(bytes, start, end) {
    var c = 0xFFFFFFFF;
    for (var i = start || 0; i < (end == null ? bytes.length : end); i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }
  function ascii(s) { var o = []; for (var i = 0; i < s.length; i++) o.push(s.charCodeAt(i) & 0x7F); return o; }
  function stringHeader(name, value) {            // header type 7 = string: u8 name-len, name, 7, u16 value-len, value
    var n = ascii(name), v = ascii(value);
    return [n.length].concat(n, [7, (v.length >> 8) & 255, v.length & 255], v);
  }
  var AUDIO_HEADERS = [].concat(stringHeader(':content-type', 'application/octet-stream'),
                                stringHeader(':event-type', 'AudioEvent'), stringHeader(':message-type', 'event'));

  /* One AudioEvent message: prelude (total length, headers length) + CRC of the prelude, headers, payload, CRC of all of it. */
  function encodeAudioEvent(payload) {
    payload = payload || new Uint8Array(0);                      // an EMPTY payload is the end-of-stream marker
    var hl = AUDIO_HEADERS.length, total = 12 + hl + payload.length + 4;
    var out = new Uint8Array(total), dv = new DataView(out.buffer);
    dv.setUint32(0, total); dv.setUint32(4, hl);
    dv.setUint32(8, crc32(out, 0, 8));
    out.set(AUDIO_HEADERS, 12);
    out.set(payload, 12 + hl);
    dv.setUint32(total - 4, crc32(out, 0, total - 4));
    return out;
  }
  function utf8(bytes) {
    var s = ''; for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    try { return decodeURIComponent(escape(s)); } catch (e) { return s; }
  }
  /* -> {headers:{name:value}, payload:Uint8Array} or null when the frame is not a well-formed message. */
  function decodeMessage(buf) {
    var b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    if (b.length < 16) return null;
    var dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
    var total = dv.getUint32(0), hl = dv.getUint32(4);
    if (total !== b.length || 12 + hl + 4 > total) return null;
    var headers = {}, i = 12, end = 12 + hl;
    while (i < end) {
      var nl = b[i++], name = utf8(b.subarray(i, i + nl)); i += nl;
      var type = b[i++];
      if (type === 7) { var vl = (b[i] << 8) | b[i + 1]; i += 2; headers[name] = utf8(b.subarray(i, i + vl)); i += vl; }
      else return null;                                           // Transcribe only sends string headers
    }
    return { headers: headers, payload: b.subarray(end, total - 4) };
  }

  /* ── 16 kHz mono 16-bit PCM ───────────────────────────────────────────────────────────────────────────── */
  function Resampler(inRate, outRate) { this.ratio = inRate / outRate; this.pos = 0; this.carry = new Float32Array(0); }
  Resampler.prototype.process = function (inp) {
    var data = new Float32Array(this.carry.length + inp.length);
    data.set(this.carry, 0); data.set(inp, this.carry.length);
    var out = [], p = this.pos, r = this.ratio;
    while (p + r <= data.length) {                                // box-average each output sample's span (a cheap low-pass)
      var s = Math.floor(p), e = Math.max(s + 1, Math.min(Math.floor(p + r), data.length)), sum = 0;
      for (var i = s; i < e; i++) sum += data[i];
      var v = Math.max(-1, Math.min(1, sum / (e - s)));
      out.push(v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7FFF));
      p += r;
    }
    var used = Math.floor(p);
    this.carry = data.slice(used); this.pos = p - used;
    return out;                                                   // plain array of int16 values
  };
  function pcmBytes(samples) {                                    // little-endian, explicitly (not platform order)
    var u = new Uint8Array(samples.length * 2), dv = new DataView(u.buffer);
    for (var i = 0; i < samples.length; i++) dv.setInt16(i * 2, samples[i], true);
    return u;
  }

  var WORKLET_SRC =
    "class P extends AudioWorkletProcessor{constructor(){super();this.b=new Float32Array(2048);this.n=0;}" +
    "process(i){var c=i[0]&&i[0][0];if(c){for(var k=0;k<c.length;k++){this.b[this.n++]=c[k];" +
    "if(this.n===2048){this.port.postMessage(this.b.slice(0));this.n=0;}}}return true;}}" +
    "registerProcessor('ca-pcm',P);";

  var FRAME_SAMPLES = 1600;           // 100 ms at 16 kHz: inside Transcribe's recommended 50-200 ms
  var QUEUE_FRAMES = 100;             // ~10 s of audio held while a socket is being (re)opened
  var DENIED = { NotAllowedError: 1, SecurityError: 1, PermissionDeniedError: 1 };

  var STALE = { stale: true };        // a start() that was cancelled before it finished: not an error, just over

  /* Count of live microphone streams, so a stream is released exactly once. */
  var micUsers = 0;
  function micAcquire(st) { if (!st || st.__caMic) return; st.__caMic = true; micUsers++; }
  function micRelease(st) { if (!st || !st.__caMic) return; st.__caMic = false; if (micUsers > 0) micUsers--; }

  function crewbossSpeech(input, mic, opts) {
    if (!mic || !input) return null;
    mic.classList.remove('hidden');                                // some callers ship the mic hidden until an engine exists
    opts = opts || {};
    var lang = opts.lang || '';
    var urlPath = opts.urlPath || '/ca/speech/url';
    var retryMs = opts.retryMs != null ? opts.retryMs : 1000;     // first reconnect back-off (doubles, capped at 5 s)
    var maxRetries = opts.maxRetries != null ? opts.maxRetries : 6;
    var stableMs = opts.stableMs != null ? opts.stableMs : 30000; // a socket must live this long to clear the retry count
    var silenceMs = opts.silenceMs != null ? opts.silenceMs : 600000;   // owner q421: stop after 10 min with no FINAL transcript

    var want = false, gen = 0, phase = 'idle';
    var prefix = '', committed = '', own = false, streak = 0, silenceTimer = 0;
    var sockets = [], active = null, connecting = false;
    var audio = null, pend = [], queue = [];

    function toast(msg) {
      if (opts.onMessage) { opts.onMessage(msg); return; }
      var was = input.getAttribute('placeholder'); input.setAttribute('placeholder', msg);
      setTimeout(function () { input.setAttribute('placeholder', was || ''); }, 5000);
    }
    function state(s) {
      phase = s;
      mic.style.color = s === 'idle' ? '' : 'rgb(var(--c-indigo))';       // terracotta from the press until it is muted
      mic.setAttribute('aria-pressed', s === 'idle' ? 'false' : 'true');
      if (opts.onState) { try { opts.onState(s); } catch (e) {} }
    }

    /* ── the text in the bar: prefix (typed) + committed (final) + each socket's live partial ──────────── */
    function words(t) { t = (t || '').trim(); return t ? t.split(/\s+/) : []; }
    function join(parts) {
      var o = '';
      parts.forEach(function (p) { p = (p || '').replace(/\s+/g, ' ').trim(); if (p) o += (o ? ' ' : '') + p; });
      return o;
    }
    function render() {
      var live = sockets.map(function (s) { return s.interim; });
      if (!committed && !live.some(Boolean)) return;                  // nothing heard yet: leave typed text exactly alone
      own = true;
      input.value = join([prefix.replace(/\s+$/, ''), committed].concat(live));
      try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}   // .value fires nothing: let the UI repaint
      own = false;
    }
    /* Typing while listening: whatever is in the box now is the new prefix and the dictation continues after it.
       A partial that was on screen is now part of that prefix, but Transcribe keeps sending the WHOLE segment, so the
       words already shown are remembered (skipWords, keyed to the segment's ResultId) and dropped from what follows
       — otherwise they would be typed a second time. The cut is by word count, because the last word of a partial is
       often revised ("angel" -> "angelina"). A new segment (new ResultId) is not affected. */
    input.addEventListener('input', function () {
      if (own) return;
      prefix = input.value; committed = '';
      sockets.forEach(function (s) {
        if (s.interim) { s.skipWords = (s.skipId === s.curId ? s.skipWords : 0) + words(s.interim).length; s.skipId = s.curId; }
        s.interim = '';
      });
    });
    function commitInterims() {
      sockets.forEach(function (s) { if (s.interim) { committed = join([committed, s.interim]); s.interim = ''; } });
    }
    function onTranscript(s, json) {
      var res = (json && json.Transcript && json.Transcript.Results) || [];
      res.forEach(function (r) {
        var alt = r.Alternatives && r.Alternatives[0], text = alt ? alt.Transcript : '', heard = !!(text || '').trim();
        s.curId = r.ResultId;
        if (s.skipWords && s.skipId === r.ResultId) text = words(text).slice(s.skipWords).join(' ');
        else s.skipWords = 0;
        if (r.IsPartial) s.interim = text || '';
        else {
          s.interim = ''; s.skipWords = 0;
          if (text) committed = join([committed, text]);
          if (heard) armSilence(gen);                               // a FINAL transcript is what resets the 10-minute stop
        }
      });
      render();
    }

    /* ── sockets ────────────────────────────────────────────────────────────────────────────────────────── */
    function failure(code, status) {
      var e = new Error(code); e.status = status;
      e.fatal = status === 401 || status === 403 || status === 404 || status === 503 || status === 422;
      return e;
    }
    function openSocket() {
      var q0 = opts.query ? (typeof opts.query === 'function' ? opts.query() : opts.query) : '';   // e.g. 'for=pa' (the PA's dictation is metered)
      return root.fetch(urlPath + '?lang=' + encodeURIComponent(lang) + (q0 ? '&' + q0 : ''), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) {
          if (!r.ok) throw failure('voice-url', r.status);
          return r.json();
        }).then(function (j) {
          return new Promise(function (resolve, reject) {
            var s = { ws: null, interim: '', timers: [], openedAt: 0, curId: undefined, skipId: undefined, skipWords: 0 }, opened = false;
            var ws;
            try { ws = new root.WebSocket(j.url); } catch (e) { reject(failure('ws', 0)); return; }
            ws.binaryType = 'arraybuffer';
            s.ws = ws;
            ws.onopen = function () { opened = true; s.openedAt = Date.now(); resolve(s); };
            ws.onmessage = function (ev) {
              var m = decodeMessage(ev.data);
              if (!m) return;
              if (m.headers[':message-type'] === 'exception') {
                try { console.warn('[voice]', m.headers[':exception-type'], utf8(m.payload)); } catch (_) {}
                return;                                               // the close that follows drives the reconnect
              }
              var json = null; try { json = JSON.parse(utf8(m.payload)); } catch (e) {}
              if (json) onTranscript(s, json);
            };
            ws.onerror = function () { if (!opened) reject(failure('ws', 0)); };
            ws.onclose = function () {
              if (!opened) { reject(failure('ws', 0)); return; }
              socketClosed(s);
            };
          });
        });
    }
    function dropSocket(s) {
      s.timers.forEach(clearTimeout); s.timers = [];
      var i = sockets.indexOf(s); if (i >= 0) sockets.splice(i, 1);
      try { s.ws.onclose = null; s.ws.onmessage = null; s.ws.close(); } catch (e) {}
    }
    function backoff() { return Math.min(5000, retryMs * Math.pow(2, Math.max(0, streak - 1))); }
    /* AWS presigned URLs only gate the HANDSHAKE, so a socket runs as long as it is wanted; there is no timed
       rotation (it split words). A close, for ANY reason, is answered by a new socket and the audio spoken meanwhile is
       replayed. The retry count is cleared only by a socket that LIVED (stableMs): open-then-close, which is what a
       Transcribe in-band exception looks like, counts against the budget and so cannot loop forever. */
    function socketClosed(s) {
      s.timers.forEach(clearTimeout); s.timers = [];
      if (s.interim) { committed = join([committed, s.interim]); s.interim = ''; }   // keep what was heard
      var i = sockets.indexOf(s); if (i >= 0) sockets.splice(i, 1);
      render();
      if (!(want && active === s)) return;
      active = null;
      var sess = gen;
      if (Date.now() - s.openedAt >= stableMs) streak = 0;
      else if (++streak > maxRetries) { fail('Voice typing could not stay connected. Press the mic to try again.'); return; }
      setTimeout(function () { if (want && gen === sess) reconnect(sess, false); }, streak ? backoff() : 0);
    }
    function attach(s) {
      sockets.push(s); active = s;
      while (queue.length) sendFrame(queue.shift());
      if (phase === 'starting') state('recording');
    }
    /* initial = the first open after the press: any failure is a toast + idle. After that, retry with back-off and
       only give up (toast, idle, text kept) when it is plainly not coming back or is refused outright. */
    function reconnect(mySess, initial) {
      if (connecting) return;
      connecting = true;
      (function go() {
        if (!want || gen !== mySess) { connecting = false; return; }
        openSocket().then(function (s) {
          connecting = false;
          if (!want || gen !== mySess) { dropSocket(s); return; }
          attach(s);
        }, function (e) {
          if (!want || gen !== mySess) { connecting = false; return; }
          if (e.fatal || initial || ++streak > maxRetries) {
            connecting = false;
            fail(e.status === 503 || e.status === 404 ? 'Voice typing is not set up on this server.'
               : e.status === 403 ? 'Voice typing is not available here.'
               : e.status === 401 ? 'Sign in again to use voice typing.'
               : e.status === 429 ? 'Too many voice starts - wait a moment and try again.'
               : 'Voice typing could not connect. Check your connection and press the mic to try again.');
            return;
          }
          setTimeout(go, backoff());
        });
      })();
    }

    /* ── audio ──────────────────────────────────────────────────────────────────────────────────────────── */
    function sendFrame(bytes) {
      if (active && active.ws.readyState === 1) { try { active.ws.send(encodeAudioEvent(bytes)); return; } catch (e) {} }
      queue.push(bytes);
      if (queue.length > QUEUE_FRAMES) queue.shift();
    }
    function onSamples(au, f32) {
      if (!want || au !== audio) return;                              // an orphaned graph can never feed the stream
      var s16 = au.resampler.process(f32);
      for (var i = 0; i < s16.length; i++) pend.push(s16[i]);
      while (pend.length >= FRAME_SAMPLES) sendFrame(pcmBytes(pend.splice(0, FRAME_SAMPLES)));
    }
    function destroyAudio(au) {
      try { if (au.node) { au.node.onaudioprocess = null; if (au.node.port) au.node.port.onmessage = null; au.node.disconnect(); } } catch (e) {}
      micRelease(au.stream); try { if (au.stream) au.stream.getTracks().forEach(function (t) { t.onended = null; t.stop(); }); } catch (e) {}
      try { if (au.ctx && au.ctx.close) au.ctx.close(); } catch (e) {}
    }
    /* Everything it builds belongs to ONE start (mySess). If the user stopped while the permission prompt or the
       worklet was still pending, whatever exists by then is torn down here and the promise rejects with STALE. */
    function openAudio(mySess) {
      var md = root.navigator && root.navigator.mediaDevices;
      if (!md || !md.getUserMedia) return Promise.reject(Object.assign(new Error('nomic'), { name: 'NotSupportedError' }));
      var au = { stream: null, ctx: null, node: null, resampler: null };
      var stale = function () { return !want || gen !== mySess; };
      var settle = function () { if (stale()) { destroyAudio(au); throw STALE; } audio = au; };
      /* opts.deviceId (a string, or a function returning one — the PA page's microphone picker): that input, exact.
         If it has gone (OverconstrainedError / NotFoundError), fall back to the default input and say so via
         opts.onDeviceGone(id) (a toast). Without it the command bar behaves exactly as before. */
      var want_id = typeof opts.deviceId === 'function' ? opts.deviceId() : opts.deviceId;
      var base = { echoCancellation: true, noiseSuppression: true, channelCount: 1 };
      var open = function (c) { return md.getUserMedia({ audio: c }); };
      var got = want_id ? open(Object.assign({ deviceId: { exact: want_id } }, base)).catch(function (e) {
        if (e && (e.name === 'OverconstrainedError' || e.name === 'NotFoundError')) {
          if (opts.onDeviceGone) { try { opts.onDeviceGone(want_id); } catch (x) {} }
          return open(base);
        }
        throw e;
      }) : open(base);
      return got.then(function (st) {
        au.stream = st; micAcquire(st);
        if (stale()) { destroyAudio(au); throw STALE; }
        var Ctx = root.AudioContext || root.webkitAudioContext;
        au.ctx = new Ctx();
        var ctx = au.ctx;
        au.resampler = new Resampler(ctx.sampleRate, 16000);
        var src = ctx.createMediaStreamSource(st);
        if (opts.stream) { try { opts.stream(st, ctx, src); } catch (e) {} }
        var gain = ctx.createGain(); gain.gain.value = 0; gain.connect(ctx.destination);   // keeps the graph pulled; silent
        var finish = function () { if (ctx.state === 'suspended' && ctx.resume) ctx.resume(); };
        var viaScript = function () {
          au.node = ctx.createScriptProcessor(4096, 1, 1);
          au.node.onaudioprocess = function (e) { onSamples(au, new Float32Array(e.inputBuffer.getChannelData(0))); };
          src.connect(au.node); au.node.connect(gain); finish();
        };
        if (ctx.audioWorklet && root.AudioWorkletNode && root.Blob && root.URL && root.URL.createObjectURL) {
          var u = root.URL.createObjectURL(new root.Blob([WORKLET_SRC], { type: 'application/javascript' }));
          return ctx.audioWorklet.addModule(u).then(function () {
            if (stale()) return;
            au.node = new root.AudioWorkletNode(ctx, 'ca-pcm');
            au.node.port.onmessage = function (e) { onSamples(au, e.data); };
            src.connect(au.node); au.node.connect(gain); finish();
          }, function () { if (!stale()) viaScript(); }).then(function () { try { root.URL.revokeObjectURL(u); } catch (e) {} }).then(settle);
        }
        viaScript();
        settle();
      });
    }

    /* ── start / stop ───────────────────────────────────────────────────────────────────────────────────── */
    function armSilence(sess) {
      clearTimeout(silenceTimer);
      silenceTimer = setTimeout(function () {
        if (want && gen === sess) fail('Mic off after 10 min of silence');
      }, silenceMs);
    }
    function teardown() {
      want = false; gen++; connecting = false;
      clearTimeout(silenceTimer);
      commitInterims();
      render();
      sockets.slice().forEach(function (s) {
        try { if (s === active) s.ws.send(encodeAudioEvent(null)); } catch (e) {}
        dropSocket(s);
      });
      sockets = []; active = null;
      if (audio) destroyAudio(audio);
      audio = null; pend = []; queue = [];
    }
    function fail(msg) { teardown(); state('idle'); toast(msg); }
    function stop() {
      if (!want && phase === 'idle') return;
      teardown(); state('idle');
    }
    function start() {
      want = true; var mySess = ++gen; streak = 0;
      prefix = input.value || ''; committed = '';
      state('starting');
      armSilence(mySess);
      openAudio(mySess).then(function () {
        if (!want || gen !== mySess) { return; }
        try { audio.stream.getTracks().forEach(function (t) { t.onended = function () { if (want && gen === mySess) fail('The microphone stopped. Press the mic to start again.'); }; }); } catch (e) {}
        reconnect(mySess, true);
      }, function (e) {
        if (e === STALE || !want || gen !== mySess) return;
        var n = e && e.name;
        try { console.warn('[voice] microphone', n, e); } catch (_) {}
        fail(DENIED[n] ? 'Microphone blocked - allow it for this site (or in the app settings) and press the mic again.'
           : n === 'NotFoundError' ? 'No microphone found.'
           : 'Microphone unavailable' + (n ? ' (' + n + ')' : '') + '.');
      });
    }

    /* SAFETY STOPS (owner q421, 2026-10-28: "Hidden tab + 10 min silence"). A mic left on is spend and a privacy
       problem, so it stops when the page is hidden (or being left) and after 10 minutes without a FINAL transcript.
       While the page is visible and speech is coming in, it never stops. The text stays in the bar. */
    function hidden() { if (want) fail('Mic off — tab hidden'); }
    if (root.document) root.document.addEventListener('visibilitychange', function () { if (root.document.hidden) hidden(); });
    root.addEventListener('pagehide', hidden);

    /* SUBMITTING OR RESETTING THE FORM ENDS THE LISTEN, for every caller of this controller: a trailing final must
       never refill a box that was just sent and cleared. Submit is captured so the listen is stopped (and the last
       partial committed into the box) BEFORE the form's own handlers read the value. */
    if (input.form) {
      input.form.addEventListener('submit', function () { stop(); }, true);
      input.form.addEventListener('reset', function () {
        stop(); prefix = ''; committed = '';
        sockets.forEach(function (s) { s.interim = ''; });
      });
    }

    return {
      recording: function () { return want; },
      stop: stop,
      toggle: function () { if (want) stop(); else start(); }
    };
  }

  /* ── the shared voice-activity meter (moved here from pa.js, dashboard #624) ──────────────────────────────── */
  /* Speech-band (300-3,400 Hz) power above an adaptive noise floor, so a fan or hum is ignored and only a voice rises.
     crewbossVoice.vad(ctx, sourceNode) -> {sample(), freeze(on), reset(), stop()}. sample() is POLLED by the caller (the
     PA's level bar on a frame, conversation mode every ~25 ms) and returns {db, floor, over}: `over` is dB above the
     floor. The floor is a slow-moving minimum (time constant ~5.5 s; it follows steady noise up, a quiet moment down
     at once). freeze(true) holds it still: conversation mode does that while the PA is speaking so its own voice
     leaking back through the mic is never learned as "the room". */
  function vad(ctx, source) {
    var an = ctx.createAnalyser();
    an.fftSize = 2048; an.smoothingTimeConstant = 0.3;
    source.connect(an);
    var buf = new Float32Array(an.frequencyBinCount), floor = null, frozen = false, last = 0;
    var hz = ctx.sampleRate / an.fftSize, lo = Math.floor(300 / hz), hi = Math.ceil(3400 / hz);
    return {
      sample: function () {
        var now = Date.now(), dt = last ? Math.min(0.25, (now - last) / 1000) : 0.016; last = now;
        an.getFloatFrequencyData(buf);
        var sum = 0, n = 0;
        for (var i = lo; i <= hi && i < buf.length; i++) { sum += Math.pow(10, buf[i] / 10); n++; }
        var db = 10 * Math.log10(sum / Math.max(1, n) + 1e-12);
        if (floor === null) floor = db;
        if (!frozen) floor = db < floor ? db : floor + (db - floor) * (1 - Math.exp(-dt / 5.5));
        return { db: db, floor: floor, over: db - floor };
      },
      freeze: function (on) { frozen = !!on; },
      reset: function () { floor = null; },
      stop: function () { try { source.disconnect(an); } catch (e) {} }
    };
  }

  var api = { crewbossSpeech: crewbossSpeech, vad: vad, encodeAudioEvent: encodeAudioEvent, decodeMessage: decodeMessage,
              Resampler: Resampler, pcmBytes: pcmBytes, crc32: crc32 };
  root.crewbossSpeech = crewbossSpeech;
  root.crewbossVoice = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
