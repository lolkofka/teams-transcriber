// Run the real injected script against both native bridges. Local outgoing
// tracks must never enter the capture graph; received audio tracks must.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ui/teams-audio.js'), 'utf8');
for (const backend of ['windows', 'mac']) {
  const messages = [], nodes = [];
  let processor;
  class Peer {
    constructor() { this.handlers = {}; this.receivers = []; }
    addEventListener(kind, handler) { this.handlers[kind] = handler; }
    addTrack(track) { this.outgoing = track; }
    getReceivers() { return this.receivers; }
    close() {}
  }
  class Context {
    constructor() { this.sampleRate = 16000; this.state = 'running'; }
    createScriptProcessor() { return processor = {connect() {}}; }
    createGain() { return {gain: {}, connect() {}}; }
    createMediaStreamSource(stream) {
      nodes.push(stream.tracks[0].id);
      return {connect() {}, disconnect() {}};
    }
  }
  const getUserMedia = async () => 'local microphone';
  const window = {RTCPeerConnection: Peer};
  const bridge = {postMessage: message => messages.push(JSON.parse(message))};
  if (backend === 'windows') window.chrome = {webview: bridge};
  else window.webkit = {messageHandlers: {teamsAudio: bridge}};
  const context = vm.createContext({window,
    location: {hostname: 'teams.microsoft.com'},
    crypto: {randomUUID: () => 'fixture-frame'},
    document: {addEventListener() {}},
    navigator: {mediaDevices: {getUserMedia}},
    AudioContext: Context,
    MediaStream: class {constructor(tracks) {this.tracks = tracks;}},
    performance: {timeOrigin: Date.now(), now: () => 0},
    btoa: binary => Buffer.from(binary, 'binary').toString('base64'),
  });
  vm.runInContext(source, context);
  const pc = new window.RTCPeerConnection();
  pc.addTrack({id: 'my-microphone', kind: 'audio'});
  assert.deepEqual(nodes, []);
  assert.equal(context.navigator.mediaDevices.getUserMedia, getUserMedia);
  pc.handlers.track({track: {id: 'remote-video', kind: 'video'}});
  assert.deepEqual(nodes, []);
  const remote = {id: 'remote-audio', kind: 'audio', readyState: 'live', addEventListener() {}};
  pc.receivers.push({track: remote});
  pc.handlers.track({track: remote});
  pc.handlers.track({track: remote}); // Duplicate notifications cannot double volume.
  assert.deepEqual(nodes, ['remote-audio']);
  processor.onaudioprocess({inputBuffer: {getChannelData: () => new Float32Array([.25, -.25])}});
  const packet = messages.find(message => message.kind === 'pcm');
  assert.equal(packet.rate, 16000);
  assert.equal(packet.tracks, 1);
  assert.equal(Buffer.from(packet.pcm, 'base64').readInt16LE(0), 8192);
  pc.close();
  assert.equal(window.__teamsAudioTap.status().tracks, 0);
}
console.log('Received audio only: Windows and Mac bridges passed');
