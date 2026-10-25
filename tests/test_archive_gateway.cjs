// Synthetic Apps Script harness. No Google credentials or external calls.
const fs=require('fs'),vm=require('vm'),crypto=require('crypto'),zlib=require('zlib'),assert=require('assert');
const secret='synthetic-test-secret-only-'+'a'.repeat(40),files=new Map(),rows=[[],[],['ID']];
rows[2][23]='Note / post-mortem';let seq=0;
const bytes=(v)=>Array.from(Buffer.isBuffer(v)?v:Buffer.from(String(v),'utf8')).map(x=>x>127?x-256:x);
const buffer=(v)=>Buffer.from(Array.from(v,x=>(x+256)%256));
const blob=(v)=>({getBytes:()=>bytes(Buffer.isBuffer(v)?v:Buffer.from(String(v))),getDataAsString:()=>Buffer.isBuffer(v)?v.toString('utf8'):String(v)});
const sheet={getLastRow:()=>rows.length,appendRow:r=>rows.push([...r]),getRange:(r,c,h=1,w=1)=>({
 getValues:()=>Array.from({length:h},(_,i)=>Array.from({length:w},(_,j)=>rows[r+i-1]?.[c+j-1]??'')),
 getValue:()=>rows[r-1]?.[c-1]??''})};
const folder={getFilesByName:n=>{const found=[...files.values()].filter(f=>f.name===n);let i=0;return {hasNext:()=>i<found.length,next:()=>found[i++]};},
 createFile:b=>{const id='synthetic-file-'+(++seq);const f={name:b.name,getId:()=>id,getBlob:()=>blob(b.data)};files.set(id,f);return f;}};
const properties={ARCHIVE_HMAC_SECRET:secret,ARCHIVE_FOLDER_ID:'folder',REGISTRY_ID:'sheet'},cache=new Map();
const context={console,Date,JSON,Array,Object,String,Number,Error,Math,
 Utilities:{Charset:{UTF_8:'utf8'},DigestAlgorithm:{SHA_256:'sha256'},newBlob:blob,
 computeDigest:(a,t)=>bytes(crypto.createHash('sha256').update(t).digest()),
 computeHmacSha256Signature:(t,k)=>bytes(crypto.createHmac('sha256',k).update(t).digest()),
 gzip:(b,n)=>({data:zlib.gzipSync(buffer(b.getBytes())),name:n}),ungzip:b=>blob(zlib.gunzipSync(buffer(b.getBytes())))},
 PropertiesService:{getScriptProperties:()=>({getProperty:n=>properties[n],setProperty:(n,v)=>{properties[n]=v;}})},
 ContentService:{MimeType:{JSON:'json'},createTextOutput:t=>({text:t,setMimeType(){return this;}})},
 LockService:{getScriptLock:()=>({waitLock:()=>{},releaseLock:()=>{}})},
 CacheService:{getScriptCache:()=>({get:k=>cache.get(k),put:(k,v)=>cache.set(k,v)})},
 DriveApp:{getFolderById:()=>folder},SpreadsheetApp:{openById:()=>({getSheetByName:()=>sheet}),flush:()=>{}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(require('path').join(__dirname,'../apps_script/ArchiveGateway.gs'),'utf8'),context);
const sha=t=>crypto.createHash('sha256').update(t).digest('hex');
function envelope(action,fields={},nonce=crypto.randomBytes(16).toString('hex')){
 const message=JSON.stringify({protocol:1,action,timestamp:Math.floor(Date.now()/1000),nonce,...fields});
 return {message,signature:crypto.createHmac('sha256',secret).update(message).digest('hex')};
}
function call(env){const out=JSON.parse(context.doPost({postData:{contents:JSON.stringify(env)}}).text);
 assert.equal(out.signature,crypto.createHmac('sha256',secret).update(out.message).digest('hex'));return JSON.parse(out.message);}
const payload=JSON.stringify({unicode:'chiusura €',rows:[1,2],missing:null}),kind='TECHNICAL_TEST',run='RUN-20261025-120000';
const fields={kind,cycle_id:run,payload_json:payload,payload_sha256:sha(payload)};fields.object_id=sha(kind+'\n'+run+'\n'+fields.payload_sha256);
let r=call(envelope('put',fields));assert.equal(r.ok,true);assert.equal(files.size,1);assert.equal(rows.length,4);
r=call(envelope('get',{object_id:fields.object_id}));assert.equal(r.payload_json,payload);
r=call(envelope('put',fields));assert.equal(r.ok,true);assert.equal(files.size,1);assert.equal(rows.length,4);
const retry=envelope('get',{object_id:fields.object_id});assert.equal(call(retry).ok,true);assert.equal(call(retry).error,'REQUEST_REPLAY');
const bad=envelope('put',fields);bad.signature='0'.repeat(64);assert.equal(call(bad).error,'AUTHENTICATION_FAILED');
assert.equal(call(envelope('put',{...fields,payload_json:'{}'})).error,'PAYLOAD_HASH_INVALID');
assert.equal(call(envelope('put',{...fields,kind:'TRADE'})).error,'KIND_OR_RUN_INVALID');
// Recovery after Drive creation succeeded but the index write was interrupted.
rows.pop();assert.equal(call(envelope('put',fields)).ok,true);assert.equal(rows.length,4);assert.equal(files.size,1);
// A signed read must not silently bless an altered index.
rows[3][23]=JSON.stringify({...JSON.parse(rows[3][23]),payload_sha256:'0'.repeat(64)});
assert.equal(call(envelope('get',{object_id:fields.object_id})).error,'REGISTRY_INDEX_CONFLICT');
assert.equal(rows[2][0],'ID');assert.equal(rows[2][23],'Note / post-mortem');
console.log('ArchiveGateway synthetic integration: PASS (put/get, idempotence, replay, tamper, crash recovery, index conflict)');

// Dedicated price cache: no per-batch Registry writes; CAS and idempotent commit.
const beforeRows=rows.length;
function cacheFields(value){const payload_json=JSON.stringify(value),payload_sha256=sha(payload_json);return {payload_json,payload_sha256,object_id:sha(value.kind+'\n'+payload_sha256)};}
let batch=cacheFields({kind:'PRICE_CACHE_BATCH',schema:1,bucket:'0',entries:[]});
assert.equal(call(envelope('cache_put',batch)).ok,true);
assert.equal(call(envelope('cache_get',{object_id:batch.object_id})).payload_json,batch.payload_json);
assert.equal(call(envelope('cache_get',{object_id:fields.object_id})).error,'REQUEST_INVALID');
let mf=cacheFields({kind:'PRICE_CACHE_MANIFEST',schema:1,buckets:{'0':batch.object_id}});
assert.equal(call(envelope('cache_commit',{...mf,base_head:null})).ok,true);
assert.equal(call(envelope('cache_head')).head.object_id,mf.object_id);
assert.equal(call(envelope('cache_commit',{...mf,base_head:null})).ok,true);
let mf2=cacheFields({kind:'PRICE_CACHE_MANIFEST',schema:1,buckets:{}});
assert.equal(call(envelope('cache_commit',{...mf2,base_head:null})).error,'CACHE_HEAD_CONFLICT');
assert.equal(rows.length,beforeRows);
const scanFields={...fields,kind:'SCAN_ARCHIVE',object_id:sha('SCAN_ARCHIVE'+'\n'+run+'\n'+fields.payload_sha256)};assert.equal(call(envelope('put',scanFields)).ok,true);
const claim=call(envelope('collector_claim',{slot_key:'2026-10-26|09:20'}));assert.equal(claim.claimed,true);
assert.equal(call(envelope('collector_claim',{slot_key:'2026-10-26|09:20'})).claimed,false);
assert.equal(call(envelope('collector_finish',{slot_key:'2026-10-26|09:20',claim_token:'wrong',object_id:fields.object_id})).error,'REQUEST_INVALID');
assert.equal(call(envelope('collector_finish',{slot_key:'2026-10-26|09:20',claim_token:claim.claim_token,object_id:scanFields.object_id})).finished,true);
assert.equal(call(envelope('collector_claim',{slot_key:'2026-10-26|09:20'})).claimed,false);
console.log('Price cache CAS, idempotence, private namespace, no Registry flood, collector dedup: PASS');
