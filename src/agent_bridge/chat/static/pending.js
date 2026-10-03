'use strict';
function addressedRecipients(text, selected, statuses) {
  const recipients = new Set(selected);
  for (const match of text.matchAll(/@(claude|codex|hermes|grok)\b/gi)) {
    const id = match[1].toLowerCase();
    const status = statuses.find(p => p.id === id);
    if (!status || status.state !== 'ready') throw new Error(id + ' is not connected yet. Your draft has been kept; it was not sent or saved as a note.');
    recipients.add(id);
  }
  return [...recipients].sort();
}
// Keep the ID when a committed request's response is lost. A changed payload is
// an intentional new request; an acknowledged request may also be sent anew.
class PendingSubmission {
  prepare(room, body) {
    const key = JSON.stringify([room, body]);
    if (!this.pending || this.pending.key !== key) {
      this.pending = {key, body: {...body, request_id: crypto.randomUUID()}};
    }
    return this.pending.body;
  }
  acknowledge() { this.pending = null; }
}

function conversationRecipients(text, mode, lead, selected, statuses) {
  if(mode==='note') return [];
  const mentions=addressedRecipients(text, [], statuses);
  const ready = new Set(statuses.filter(p => p.state === 'ready').map(p => p.id));
  if (!ready.has(lead)) throw new Error(lead+' is not connected. Choose another agent or reconnect it.');
  let recipients=mode==='discuss' ? [...new Set([...selected, lead, ...mentions])].sort() : (mentions.length ? mentions : [lead]);
  if (mode === 'discuss') recipients = recipients.filter(id => id === lead || ready.has(id));
  for(const id of recipients) if(!statuses.some(p=>p.id===id && p.state==='ready')) throw new Error(id+' is not connected. Choose another agent or reconnect it.');
  if(mode==='discuss' && recipients.length<2) throw new Error('Choose at least two agents for a discussion.');
  return recipients;
}
function handleComposerKey(event, busy, submit) {
  if(event.key!=='Enter' || event.shiftKey || event.isComposing || event.keyCode===229) return;
  event.preventDefault();
  if(!busy && !event.repeat) submit();
}
