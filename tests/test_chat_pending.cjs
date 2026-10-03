const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const context = vm.createContext({crypto});
vm.runInContext(fs.readFileSync('src/agent_bridge/chat/static/pending.js', 'utf8'), context);
vm.runInContext(`
 const queue = new PendingSubmission();
 const body = {text:'hello',recipients:['claude'],classification:'public'};
 const first = queue.prepare('room',body);
 // The server committed the first request, but the browser lost its response.
 const retried = queue.prepare('room',body);
 if(first.request_id !== retried.request_id) throw new Error('Uncertain retry duplicated the request');
 queue.acknowledge();
 if(queue.prepare('room',body).request_id === first.request_id) throw new Error('New intentional send reused an acknowledged ID');
 if(queue.prepare('other-room',body).request_id === retried.request_id) throw new Error('Wrong-room request reused');
`, context);
console.log('Pending send retry tests passed');
vm.runInContext(`
 const statuses = [{id:'codex',state:'verification_required'},{id:'claude',state:'ready'},{id:'hermes',state:'ready'},{id:'grok',state:'ready'}];
 let rejected=false;
 try { addressedRecipients('@codex are you there?', [], statuses); } catch(e) { rejected=true; }
 if(!rejected) throw new Error('Unavailable mention silently saved as note');
 if(addressedRecipients('@claude hello', [], statuses)[0]!=='claude') throw new Error('Ready mention was not addressed');
 if(addressedRecipients('@hermes hello', [], statuses)[0]!=='hermes') throw new Error('Ready Hermes mention was not addressed');
 if(addressedRecipients('@grok hello', [], statuses)[0]!=='grok') throw new Error('Ready Grok mention was not addressed');
 if(addressedRecipients('Just a note', [], statuses).length!==0) throw new Error('Plain note dispatched inference');
`, context);
vm.runInContext(`
 const ready = ['claude','codex','hermes','grok'].map(id=>({id,state:'ready'}));
 if(conversationRecipients('hello','chat','codex',['claude'],ready).join()!=='codex') throw Error('Lead routing failed');
 if(conversationRecipients('@claude help','chat','codex',[],ready).join()!=='claude') throw Error('Mention should override lead');
 if(conversationRecipients('hello','discuss','codex',['claude'],ready).join()!=='claude,codex') throw Error('Discussion must include lead');
 if(conversationRecipients('note','note','codex',[],ready).length) throw Error('Note dispatched');
`,context);
vm.runInContext(`
 let sent=0, prevented=0;
 const submit=()=>sent++;
 const press=extra=>handleComposerKey({key:'Enter',preventDefault:()=>prevented++,...extra},false,submit);
 press({});press({shiftKey:true});press({isComposing:true});press({repeat:true});
 handleComposerKey({key:'Enter',preventDefault:()=>prevented++},true,submit);
 if(sent!==1)throw Error('Enter must send once; Shift, IME, repeat and busy must not send');
`,context);
