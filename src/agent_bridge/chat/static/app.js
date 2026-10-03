'use strict';
const roundLink = document.createElement('a');
roundLink.href = '/peer-rounds';
roundLink.textContent = 'Review peer round requests';
document.querySelector('header').append(roundLink);
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) { sessionStorage.setItem('room-token', fragment.get('token')); history.replaceState(null, '', '/'); }
let roomId = null, lastMessages = '', sending = false, participantStates = [], preferencesRoom = null;
const selected = new Set();
const pendingSubmission = new PendingSubmission();
function node(tag, text, className) { const n = document.createElement(tag); if(text !== undefined) n.textContent=text; if(className) n.className=className; return n; }
async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer '+(sessionStorage.getItem('room-token') || ''), 'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json(); if(!response.ok) throw new Error(data.error || 'Request failed'); return data;
}
function error(e) { $('error').textContent=e.message; }
function updateLeadOptions(current) {
  const select = $('lead');
  select.replaceChildren();
  for (const p of participantStates) {
    const option = node('option', p.id);
    option.value = p.id;
    option.disabled = p.state !== 'ready';
    select.append(option);
  }
  if (current && participantStates.some(p => p.id === current)) select.value = current;
  else if (participantStates.some(p => p.state === 'ready')) select.value = participantStates.find(p => p.state === 'ready').id;
}
async function loadRooms() {
  const rooms=await api('/api/rooms'); $('rooms').replaceChildren();
  if(!rooms.some(r=>r.id===roomId)){roomId=rooms.length?rooms[0].id:null;lastMessages='';preferencesRoom=null;}
  if(!roomId){$('title').textContent='Your conversations';$('messages').replaceChildren(node('p','Create a new conversation to start.'));$('jobs').replaceChildren();}
  for(const room of rooms) { const b=node('button',room.title, room.id===roomId?'active':''); b.onclick=()=>{roomId=room.id;lastMessages='';loadRooms().then(refresh).catch(error);}; const row=node('div',undefined,'room-row');b.classList.add('room-title');row.append(b);for(const action of ['Rename','Delete']){const control=node('button',action==='Rename'?'✎':'×','room-action');control.setAttribute('aria-label',action+' '+room.title);control.title=action+' chat';control.onclick=()=>editRoom(room,action);row.append(control);}$('rooms').append(row); if(room.id===roomId)$('title').textContent=room.title; }
}
async function refresh() {
  const status=await api('/api/participants'); $('participants').replaceChildren(); $('recipients').replaceChildren();
  participantStates=status.participants;
  for(const p of status.participants) {
    const card=node('div',undefined,'participant '+p.state); card.append(node('strong',p.id),node('small',p.state.replaceAll('_',' '))); card.title=p.detail||'';
    if(['claude','codex','hermes','grok'].includes(p.id)) { const reset=node('button','Fresh consultation'); reset.title='Starts a new agent session with saved room history.'; reset.onclick=async()=>{try {await api('/api/rooms/'+roomId+'/reset',{target:p.id});$('error').textContent='Next request starts fresh with the saved room history.';}catch(e){error(e);}};card.append(reset); }
    $('participants').append(card);
    const label=node('label'); const input=node('input');input.type='checkbox';input.value=p.id;input.disabled=p.state!=='ready'; input.checked=selected.has(p.id);input.onchange=()=>{input.checked?selected.add(p.id):selected.delete(p.id);savePreferences();};label.append(input,document.createTextNode(' '+p.id));$('recipients').append(label);
  }
  if(!roomId){$('send').disabled=true;return;}
  const refreshingRoom=roomId;const snapshot=await api('/api/rooms/'+refreshingRoom);if(roomId!==refreshingRoom)return;
  updateLeadOptions(snapshot.preferences.lead);
  if(preferencesRoom!==roomId){preferencesRoom=roomId;const ready=new Set(participantStates.filter(p=>p.state==='ready').map(p=>p.id));selected.clear();snapshot.preferences.participants.filter(p=>ready.has(p)).forEach(p=>selected.add(p));$('mode').value='chat';updateMode();for(const box of $('recipients').querySelectorAll('input'))box.checked=selected.has(box.value);}
  const busy=snapshot.jobs.some(j=>j.status==='queued'||j.status==='running');$('send').disabled=sending||busy;
  const encoded=JSON.stringify(snapshot.messages);
  if(encoded!==lastMessages){lastMessages=encoded;$('messages').replaceChildren();
    if(!snapshot.messages.length){const empty=node('div',undefined,'empty');empty.append(node('h2','Bring your agents together.'),node('p','Choose your lead and start talking. Invite more perspectives with Discuss together.'));$('messages').append(empty);}
    for(const m of snapshot.messages){const article=node('article',undefined,'message '+(m.author==='Human'?'human':''));article.append(node('div',m.author+' · '+m.classification+' · '+new Date(m.created*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),'meta'),node('div',m.text,'text'));$('messages').append(article);}
    $('messages').scrollTop=$('messages').scrollHeight;
  }
  $('jobs').replaceChildren();for(const j of snapshot.jobs.filter(j=>j.status!=='completed').slice(-5))$('jobs').append(node('div',j.target+' · '+j.status+(j.error?' — '+j.error:''),'job'));
}
$('new-room').onclick=()=>{$('new-room-form').hidden=!$('new-room-form').hidden;if(!$('new-room-form').hidden)$('room-name').focus();};
$('new-room-form').onsubmit=async event=>{event.preventDefault();const title=$('room-name').value.trim();if(!title)return;try{roomId=(await api('/api/rooms',{title})).id;lastMessages='';$('new-room-form').hidden=true;$('room-name').value='';await loadRooms();await refresh();}catch(e){error(e);}};
function updateMode(){const mode=$('mode').value;$('discussion-options').hidden=mode!=='discuss';$('send').textContent=mode==='discuss'?'Start discussion':mode==='note'?'Save note':'Send message ↗';$('mode-hint').textContent=mode==='discuss'?'One reply per participant, then a lead summary. At most five replies; then everyone waits for you.':mode==='note'?'Save to room history without requesting a reply.':'Your lead replies. @mentions select another agent for this message.';}
async function savePreferences(){try{await api('/api/rooms/'+roomId+'/preferences',{lead:$('lead').value,participants:[...selected]});}catch(e){error(e);}}
$('lead').onchange=savePreferences;
$('mode').onchange=updateMode;
$('composer').onsubmit=async event=>{event.preventDefault();if(sending||!roomId||!$('draft').value.trim())return;sending=true;$('send').disabled=true;$('error').textContent='';
  const text=$('draft').value;try{const mode=$('mode').value;const recipients=conversationRecipients(text,mode,$('lead').value,[...selected],participantStates);await api('/api/rooms/'+roomId+'/messages',pendingSubmission.prepare(roomId,{text,recipients,classification:$('classification').value,mode,lead:$('lead').value}));pendingSubmission.acknowledge();$('mode').value='chat';updateMode();if($('draft').value===text)$('draft').value='';await refresh();}catch(e){error(e);}finally{sending=false;await refresh().catch(error);}};
$('stop').onclick=async()=>{try{await api('/api/rooms/'+roomId+'/stop',{});await refresh();}catch(e){error(e);}};
async function poll(){try{await loadRooms();await refresh();}catch(e){error(e);}setTimeout(poll,2500);}
loadRooms().then(poll).catch(error);
$('draft').addEventListener('keydown',event=>handleComposerKey(event,sending||$('send').disabled,()=>$('composer').requestSubmit()));

let editingRoom=null, roomEditBusy=false;
function editRoom(room,action){editingRoom={id:room.id,action};$('room-dialog-title').textContent=action+' chat';$('room-dialog-description').textContent=action==='Delete'?'Delete "'+room.title+'" and its Agent Room history? This cannot be undone. Pending replies will be discarded. Provider histories and diagnostic files are not erased.':'';$('room-edit-name').hidden=action==='Delete';$('room-edit-name').required=action==='Rename';$('room-edit-name').value=room.title;$('room-dialog-save').textContent=action==='Delete'?'Delete chat':'Save name';$('room-dialog-error').textContent='';$('room-dialog').showModal();if(action==='Rename')$('room-edit-name').focus();}
$('room-dialog-cancel').onclick=()=>$('room-dialog').close();
$('room-edit-form').onsubmit=async event=>{event.preventDefault();if(roomEditBusy||!editingRoom)return;roomEditBusy=true;$('room-dialog-save').disabled=true;const edit={...editingRoom};try{await api('/api/rooms/'+edit.id+'/'+(edit.action==='Delete'?'delete':'rename'),edit.action==='Delete'?{confirm:true}:{title:$('room-edit-name').value});if(edit.action==='Delete'&&roomId===edit.id){roomId=null;preferencesRoom=null;lastMessages='';$('draft').value='';pendingSubmission.acknowledge();}$('room-dialog').close();await loadRooms();await refresh();}catch(e){$('room-dialog-error').textContent=e.message;}finally{roomEditBusy=false;$('room-dialog-save').disabled=false;}};
