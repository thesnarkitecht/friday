/**
 * The glasses' link to FRIDAY. The phone app and the glasses are both clients of the FRIDAY
 * home server, so pairing just means: same server, same token, same live conversation.
 *
 * Two transports, one message protocol (see server/friday/app.py):
 * - paired with a FRIDAY server: a WebSocket to /ws
 * - inside the browser preview: postMessage to the parent page, which plays the phone + server
 */
import {useSyncExternalStore} from 'react';

export type Line = {id: string; who: 'you' | 'friday' | 'heard'; text: string};
export type Alert = {id: string; kind: string; text: string; at: number};
export type LinkState = {
  status: 'pairing' | 'connected' | 'offline' | 'unpaired';
  lines: Line[];
  facts: string[];
  alerts: Alert[];
  screen: string | null; // phone screen frame (data URL) while screen sharing is on
  thinking: boolean;
};

let state: LinkState = {status: 'offline', lines: [], facts: [], alerts: [], screen: null, thinking: false};
const listeners = new Set<() => void>();
function set(patch: Partial<LinkState>) {
  state = {...state, ...patch};
  listeners.forEach(l => l());
}
const id = () => Math.random().toString(36).slice(2);
const line = (who: Line['who'], text: string): Line => ({id: id(), who, text});
const push = (l: Line) => [...state.lines, l].slice(-40);
const inPreview = window.parent !== window;
let socket: WebSocket | null = null;

type Incoming =
  | {type: 'heard'; text: string; addressed: boolean}
  | {type: 'say'; text: string}
  | {type: 'notify'; kind: string; text: string}
  | {type: 'memory'; facts: string[]}
  | {type: 'screen'; frame: string | null}
  | {type: 'thinking'; on: boolean}
  | {type: 'paired'; on: boolean};

export type IncomingHandler = (msg: Incoming) => void;
let onIncoming: IncomingHandler | null = null;
export function setIncomingHandler(fn: IncomingHandler) { onIncoming = fn; }

function receive(msg: Incoming) {
  switch (msg.type) {
    case 'heard':
      set({lines: push(line(msg.addressed ? 'you' : 'heard', msg.text)), thinking: msg.addressed});
      break;
    case 'say':
      set({lines: push(line('friday', msg.text)), thinking: false});
      break;
    case 'notify':
      set({alerts: [{id: id(), kind: msg.kind, text: msg.text, at: Date.now()}, ...state.alerts].slice(0, 20),
        lines: push(line('friday', msg.text))});
      break;
    case 'memory': set({facts: msg.facts}); break;
    case 'screen': set({screen: msg.frame}); break;
    case 'thinking': set({thinking: msg.on}); break;
    case 'paired': set({status: msg.on ? 'connected' : 'unpaired'}); break;
  }
  onIncoming?.(msg);
}

function storage(key: string, value?: string): string | null {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key);
  } catch { return null; }
}

async function claimPairingCode(code: string): Promise<boolean> {
  set({status: 'pairing'});
  const r = await fetch('../api/pair/claim', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code})}).catch(() => null);
  if (!r?.ok) { set({status: 'unpaired'}); return false; }
  storage('friday.token', (await r.json()).token);
  history.replaceState(null, '', location.pathname);  // don't leave a spent code in the address
  return true;
}

function connectSocket(token: string) {
  const url = new URL('../ws', location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', token);
  socket = new WebSocket(url);
  socket.onopen = () => set({status: 'connected'});
  socket.onmessage = e => { try { receive(JSON.parse(e.data)); } catch { /* ignore malformed frames */ } };
  socket.onclose = ev => {
    socket = null;
    if (ev.code === 4401) { storage('friday.token', ''); set({status: 'unpaired'}); return; }
    set({status: 'offline'});
    setTimeout(() => connectSocket(token), 3000);  // home box rebooted or Wi-Fi blipped
  };
}

export async function start() {
  if (inPreview) {
    window.addEventListener('message', e => {
      if (e.source === window.parent && e.data && typeof e.data.type === 'string') receive(e.data);
    });
    set({status: 'unpaired'});  // the simulated phone pairs us, like the real QR flow
    window.parent.postMessage({type: 'glasses-ready'}, '*');
    return;
  }
  const code = new URLSearchParams(location.search).get('pair');
  if (code && !(await claimPairingCode(code))) return;
  const token = storage('friday.token');
  if (!token) { set({status: 'unpaired'}); return; }
  connectSocket(token);
}

/** Typed on the glasses: always meant for FRIDAY, always the owner. */
export function say(text: string) {
  const msg = {type: 'utterance', text: /\bfriday\b/i.test(text) ? text : `Friday, ${text}`, speaker: 'owner'};
  if (inPreview) window.parent.postMessage(msg, '*');
  else socket?.send(JSON.stringify(msg));
}

export function useLink(): LinkState {
  return useSyncExternalStore(cb => { listeners.add(cb); return () => listeners.delete(cb); }, () => state);
}
