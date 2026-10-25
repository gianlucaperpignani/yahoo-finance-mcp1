/** Version 1.2.0 — private immutable archive + index in existing Registro.
 * Script Properties: ARCHIVE_HMAC_SECRET, ARCHIVE_FOLDER_ID, REGISTRY_ID.
 * No broker APIs, orders, risk calculations or changes to existing records.
 */
const ARCHIVE_VERSION = '1.2.0';
const ARCHIVE_LIMIT = 2000000;
const ARCHIVE_KINDS = ['SCAN_ARCHIVE','COHORT_STORE','LEARNING_STATE','FEEDBACK',
  'RISK_BUNDLE','CHECKPOINT','RAW_MARKET_DATA','TECHNICAL_TEST'];

function archiveHex(bytes) {
  return bytes.map(x => ('0'+((x+256)%256).toString(16)).slice(-2)).join('');
}
function archiveSha(text) {
  return archiveHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,text,Utilities.Charset.UTF_8));
}
function archiveEqual(a,b) {
  if (typeof a!=='string' || typeof b!=='string' || a.length!==b.length) return false;
  let delta=0;for(let i=0;i<a.length;i++) delta|=a.charCodeAt(i)^b.charCodeAt(i);
  return delta===0;
}
function archiveSign(text,secret) {
  return archiveHex(Utilities.computeHmacSha256Signature(text,secret,Utilities.Charset.UTF_8));
}
function archiveReply(value,nonce,secret) {
  const message=JSON.stringify(Object.assign({version:ARCHIVE_VERSION,nonce:nonce},value));
  return ContentService.createTextOutput(JSON.stringify({message:message,signature:archiveSign(message,secret)}))
    .setMimeType(ContentService.MimeType.JSON);
}
function archiveAssert(ok,code) { if(!ok) throw new Error(code); }
function archiveLoad(file) {
  const record=JSON.parse(Utilities.ungzip(file.getBlob()).getDataAsString('UTF-8'));
  archiveAssert(archiveSha(record.payload_json)===record.payload_sha256,'STORED_HASH_INVALID');
  return record;
}
function archiveFile(folder,key) {
  const files=folder.getFilesByName('etoro-archive-'+key+'.json.gz');
  if(!files.hasNext()) return null;
  const file=files.next();archiveAssert(!files.hasNext(),'DUPLICATE_ARCHIVE_OBJECT');return file;
}
function archiveIndex(sheet,record,file) {
  const last=sheet.getLastRow();archiveAssert(last<10000,'REGISTRY_CAPACITY_REVIEW_REQUIRED');
  const notes=last>=4?sheet.getRange(4,24,last-3,1).getValues():[];
  const matches=[];
  notes.forEach((row,i)=>{
    try {
      const v=JSON.parse(row[0]);
      if(v.kind==='PERSISTENT_ARCHIVE_RECEIPT' && v.object_id===record.object_id)matches.push(i+4);
    }catch(ignore){}
  });
  archiveAssert(matches.length<=1,'DUPLICATE_REGISTRY_INDEX');
  if(matches.length) {
    const row=matches[0],index=JSON.parse(sheet.getRange(row,24).getValue());
    archiveAssert(index.file_id===file.getId() && index.payload_sha256===record.payload_sha256,'REGISTRY_INDEX_CONFLICT');
    return 'Registro!A'+row+':Z'+row;
  }
  let max=0;
  if(last>=4)sheet.getRange(4,1,last-3,1).getValues().forEach(r=>{
    const m=String(r[0]).match(/^REG-(\d+)(?:-P\d+)?$/);if(m)max=Math.max(max,Number(m[1]));
  });
  const row=Array(26).fill('');row[0]='REG-'+String(max+1).padStart(4,'0');
  row[1]=new Date().toISOString();row[2]='CONTROLLO TECNICO';row[4]='NESSUNA OPERAZIONE AFFIDABILE';
  row[22]='NESSUNA AZIONE';row[23]=JSON.stringify({kind:'PERSISTENT_ARCHIVE_RECEIPT',
    version:ARCHIVE_VERSION,cycle_id:record.cycle_id,object_id:record.object_id,
    payload_sha256:record.payload_sha256,file_id:file.getId(),source_kind:record.kind});
  sheet.appendRow(row);SpreadsheetApp.flush();
  const indexRow=sheet.getLastRow();
  archiveAssert(sheet.getRange(indexRow,24).getValue()===row[23],'REGISTRY_WRITE_READBACK_FAILED');
  return 'Registro!A'+indexRow+':Z'+indexRow;
}
// Price-cache objects stay in the technical Drive archive, not one Registry row
// per cron tick. Head moves only after verified bytes and compare-and-swap.
function priceCacheHandle(req,props) {
  const folder=DriveApp.getFolderById(props.getProperty('ARCHIVE_FOLDER_ID'));
  const head=JSON.parse(props.getProperty('PRICE_CACHE_HEAD')||'null');
  if(req.action==='cache_head')return {ok:true,head:head};
  if(req.action==='collector_claim') {
    archiveAssert(/^\d{4}-\d{2}-\d{2}\|(09:20|16:00)$/.test(req.slot_key),'REQUEST_INVALID');
    const claims=JSON.parse(props.getProperty('COLLECTOR_CLAIMS')||'{}');
    const old=claims[req.slot_key],now=Math.floor(Date.now()/1000);
    if(old && (old.finished || now-old.at<1200))return {ok:true,claimed:false,previous:old};
    Object.keys(claims).sort().slice(0,Math.max(0,Object.keys(claims).length-13)).forEach(k=>delete claims[k]);
    claims[req.slot_key]={at:now,finished:false,claim_token:req.nonce};props.setProperty('COLLECTOR_CLAIMS',JSON.stringify(claims));
    return {ok:true,claimed:true,claim_token:req.nonce};
  }
  if(req.action==='collector_finish') {
    archiveAssert(/^\d{4}-\d{2}-\d{2}\|(09:20|16:00)$/.test(req.slot_key),'REQUEST_INVALID');
    const claims=JSON.parse(props.getProperty('COLLECTOR_CLAIMS')||'{}');
    archiveAssert(!!claims[req.slot_key] && claims[req.slot_key].claim_token===req.claim_token,'REQUEST_INVALID');
    archiveAssert(/^[0-9a-f]{64}$/.test(req.object_id),'OBJECT_ID_INVALID');
    const file=archiveFile(folder,req.object_id);archiveAssert(!!file,'ARCHIVE_OBJECT_NOT_FOUND');
    archiveAssert(archiveLoad(file).kind==='SCAN_ARCHIVE','REQUEST_INVALID');
    claims[req.slot_key]={at:claims[req.slot_key].at,finished:true,object_id:req.object_id};
    props.setProperty('COLLECTOR_CLAIMS',JSON.stringify(claims));return {ok:true,finished:true};
  }
  archiveAssert(/^[0-9a-f]{64}$/.test(req.object_id||''),'OBJECT_ID_INVALID');
  let file=archiveFile(folder,req.object_id);
  if(req.action==='cache_put' || req.action==='cache_commit') {
    archiveAssert(typeof req.payload_json==='string' && Utilities.newBlob(req.payload_json).getBytes().length<=ARCHIVE_LIMIT,'PAYLOAD_TOO_LARGE');
    archiveAssert(archiveSha(req.payload_json)===req.payload_sha256,'PAYLOAD_HASH_INVALID');
    const kind=req.action==='cache_commit'?'PRICE_CACHE_MANIFEST':'PRICE_CACHE_BATCH';
    archiveAssert(archiveSha(kind+'\n'+req.payload_sha256)===req.object_id,'OBJECT_HASH_INVALID');
    const payload=JSON.parse(req.payload_json);
    archiveAssert(payload.kind===kind && payload.schema===1,'REQUEST_INVALID');
    if(kind==='PRICE_CACHE_BATCH')archiveAssert(Array.isArray(payload.entries) && payload.entries.length<=80,'REQUEST_INVALID');
    if(kind==='PRICE_CACHE_MANIFEST') {
      archiveAssert(payload.buckets && Object.keys(payload.buckets).length<=32,'REQUEST_INVALID');
      Object.values(payload.buckets).forEach(id=>{
        archiveAssert(/^[0-9a-f]{64}$/.test(id),'OBJECT_ID_INVALID');
        const part=archiveFile(folder,id);archiveAssert(!!part && archiveLoad(part).kind==='PRICE_CACHE_BATCH','ARCHIVE_OBJECT_NOT_FOUND');
      });
      archiveAssert((head?head.object_id:null)===(req.base_head||null) || (head && head.object_id===req.object_id),'CACHE_HEAD_CONFLICT');
    }
    const record={kind:kind,object_id:req.object_id,payload_sha256:req.payload_sha256,payload_json:req.payload_json};
    if(file)archiveAssert(JSON.stringify(archiveLoad(file))===JSON.stringify(record),'IMMUTABLE_OBJECT_CONFLICT');
    else file=folder.createFile(Utilities.gzip(Utilities.newBlob(JSON.stringify(record),'application/json'),'etoro-archive-'+req.object_id+'.json.gz'));
    const check=archiveLoad(file);archiveAssert(check.object_id===req.object_id,'DRIVE_READBACK_FAILED');
    if(kind==='PRICE_CACHE_MANIFEST')props.setProperty('PRICE_CACHE_HEAD',JSON.stringify({object_id:req.object_id,payload_sha256:req.payload_sha256}));
  }
  archiveAssert(!!file,'ARCHIVE_OBJECT_NOT_FOUND');
  const record=archiveLoad(file);
  archiveAssert(['PRICE_CACHE_MANIFEST','PRICE_CACHE_BATCH'].includes(record.kind),'REQUEST_INVALID');
  archiveAssert(archiveSha(record.kind+'\n'+record.payload_sha256)===req.object_id,'OBJECT_HASH_INVALID');
  return {ok:true,object_id:req.object_id,payload_sha256:record.payload_sha256,payload_json:record.payload_json,file_id:file.getId()};
}
// Private learning state is a separate namespace from publicly downloadable scans.
function learningHandle(req,props) {
  const folder=DriveApp.getFolderById(props.getProperty('ARCHIVE_FOLDER_ID'));
  const head=JSON.parse(props.getProperty('LEARNING_HEAD')||'null');
  if(req.action==='learning_scan_refs') {
    const claims=JSON.parse(props.getProperty('COLLECTOR_CLAIMS')||'{}');
    return {ok:true,refs:Object.values(claims).filter(c=>c.finished).map(c=>c.object_id)};
  }
  if(req.action==='learning_tickets') {
    const sheet=SpreadsheetApp.openById(props.getProperty('REGISTRY_ID')).getSheetByName('Registro');
    const last=sheet.getLastRow();archiveAssert(last<=10000,'REGISTRY_CAPACITY_REVIEW_REQUIRED');
    const found=[];sheet.getRange(4,24,Math.max(1,last-3),1).getValues().forEach(row=>{try{
      const v=JSON.parse(row[0]);if(v.proposal && v.proposal_hash && v.execution_receipt)found.push(v);
    }catch(ignore){}});
    archiveAssert(found.length<=100,'PAYLOAD_TOO_LARGE');return {ok:true,tickets:found};
  }
  if(req.action==='learning_bootstrap') {
    const files=folder.getFilesByName('etoro-learning-bootstrap-v1.json');
    archiveAssert(files.hasNext(),'LEARNING_BOOTSTRAP_REQUIRED');const file=files.next();
    archiveAssert(!files.hasNext(),'LEARNING_BOOTSTRAP_AMBIGUOUS');
    const config=JSON.parse(file.getBlob().getDataAsString('UTF-8'));
    archiveAssert(config.schema===1 && /^[0-9a-f]{64}$/.test(config.account_fingerprint),'REQUEST_INVALID');
    const sheet=SpreadsheetApp.openById(props.getProperty('REGISTRY_ID')).getSheetByName('Registro');
    const last=sheet.getLastRow();archiveAssert(last<=10000,'REGISTRY_CAPACITY_REVIEW_REQUIRED');
    const values=sheet.getRange(4,24,Math.max(1,last-3),1).getValues();let parts=[],sha=null;
    for(let i=values.length-1;i>=0;i--){try{const c=JSON.parse(values[i][0]);
      if(c.kind==='SCHEDULED_COLLECTOR_INTEGRATION'){
        if(!sha)sha=c.object_sha256;if(c.object_sha256===sha)parts.push(c);
      }}catch(ignore){}}
    archiveAssert(parts.length>0,'LEARNING_BOOTSTRAP_REQUIRED');
    return {ok:true,config:config,chunks:parts};
  }
  if(req.action==='learning_head' && !head)return {ok:true,object_id:null};
  if(req.action==='learning_head')req.object_id=head.object_id;
  archiveAssert(/^[0-9a-f]{64}$/.test(req.object_id||''),'OBJECT_ID_INVALID');
  let file=archiveFile(folder,req.object_id);
  if(req.action==='learning_put' || req.action==='learning_commit') {
    archiveAssert(typeof req.payload_json==='string' && Utilities.newBlob(req.payload_json).getBytes().length<=ARCHIVE_LIMIT,'PAYLOAD_TOO_LARGE');
    archiveAssert(archiveSha(req.payload_json)===req.payload_sha256,'PAYLOAD_HASH_INVALID');
    const kind=req.action==='learning_commit'?'COLLECTOR_STATE':'COLLECTOR_DATA';
    archiveAssert(archiveSha(kind+'\n'+req.payload_sha256)===req.object_id,'OBJECT_HASH_INVALID');
    const value=JSON.parse(req.payload_json);archiveAssert(value.kind===kind && value.schema===1,'REQUEST_INVALID');
    if(kind==='COLLECTOR_STATE')archiveAssert((head?head.object_id:null)===(req.base_head||null)||(head&&head.object_id===req.object_id),'LEARNING_HEAD_CONFLICT');
    const record={kind:kind,object_id:req.object_id,payload_sha256:req.payload_sha256,payload_json:req.payload_json};
    if(file)archiveAssert(JSON.stringify(archiveLoad(file))===JSON.stringify(record),'IMMUTABLE_OBJECT_CONFLICT');
    else file=folder.createFile(Utilities.gzip(Utilities.newBlob(JSON.stringify(record),'application/json'),'etoro-archive-'+req.object_id+'.json.gz'));
    archiveAssert(archiveLoad(file).object_id===req.object_id,'DRIVE_READBACK_FAILED');
    if(kind==='COLLECTOR_STATE')props.setProperty('LEARNING_HEAD',JSON.stringify({object_id:req.object_id,payload_sha256:req.payload_sha256}));
  }
  archiveAssert(!!file,'ARCHIVE_OBJECT_NOT_FOUND');const record=archiveLoad(file);
  const allowed=req.action==='learning_get'?['COLLECTOR_DATA']:['COLLECTOR_STATE','COLLECTOR_DATA'];
  archiveAssert(allowed.includes(record.kind),'REQUEST_INVALID');
  archiveAssert(archiveSha(record.kind+'\n'+record.payload_sha256)===req.object_id,'OBJECT_HASH_INVALID');
  return {ok:true,object_id:req.object_id,payload_sha256:record.payload_sha256,payload_json:record.payload_json,file_id:file.getId()};
}
function archiveHandle(req,props) {
  archiveAssert(req.protocol===1,'REQUEST_INVALID');
  if(['learning_head','learning_put','learning_commit','learning_get','learning_bootstrap','learning_scan_refs','learning_tickets'].includes(req.action))return learningHandle(req,props);
  if(['cache_head','cache_get','cache_put','cache_commit','collector_claim','collector_finish'].includes(req.action))return priceCacheHandle(req,props);
  archiveAssert(['status','put','get'].includes(req.action),'REQUEST_INVALID');
  const folder=DriveApp.getFolderById(props.getProperty('ARCHIVE_FOLDER_ID'));
  const book=SpreadsheetApp.openById(props.getProperty('REGISTRY_ID'));
  const sheet=book.getSheetByName('Registro');archiveAssert(!!sheet,'REGISTRO_NOT_FOUND');
  archiveAssert(sheet.getRange(3,1).getValue()==='ID' && sheet.getRange(3,24).getValue()==='Note / post-mortem','REGISTRY_SCHEMA_MISMATCH');
  if(req.action==='status') return {ok:true,configured:true,durable_storage_verified:false};
  archiveAssert(typeof req.object_id==='string' && /^[0-9a-f]{64}$/.test(req.object_id),'OBJECT_ID_INVALID');
  let file=archiveFile(folder,req.object_id),record;
  if(req.action==='put') {
    archiveAssert(ARCHIVE_KINDS.includes(req.kind) && /^RUN-\d{8}-\d{6}$/.test(req.cycle_id),'KIND_OR_RUN_INVALID');
    archiveAssert(typeof req.payload_json==='string','PAYLOAD_INVALID');
    archiveAssert(Utilities.newBlob(req.payload_json).getBytes().length<=ARCHIVE_LIMIT,'PAYLOAD_TOO_LARGE');
    JSON.parse(req.payload_json);
    archiveAssert(archiveSha(req.payload_json)===req.payload_sha256,'PAYLOAD_HASH_INVALID');
    archiveAssert(archiveSha(req.kind+'\n'+req.cycle_id+'\n'+req.payload_sha256)===req.object_id,'OBJECT_HASH_INVALID');
    record={kind:req.kind,cycle_id:req.cycle_id,object_id:req.object_id,
      payload_sha256:req.payload_sha256,payload_json:req.payload_json};
    if(file)archiveAssert(JSON.stringify(archiveLoad(file))===JSON.stringify(record),'IMMUTABLE_OBJECT_CONFLICT');
    else file=folder.createFile(Utilities.gzip(Utilities.newBlob(JSON.stringify(record),'application/json'),
      'etoro-archive-'+req.object_id+'.json.gz'));
  }else {
    archiveAssert(!!file,'ARCHIVE_OBJECT_NOT_FOUND');record=archiveLoad(file);
  }
  // Verify Drive bytes, including after creation or recovery after interrupted indexing.
  const persisted=archiveLoad(file);
  archiveAssert(persisted.object_id===req.object_id && persisted.payload_sha256===record.payload_sha256,'DRIVE_READBACK_FAILED');
  const range=archiveIndex(sheet,persisted,file);
  const result={ok:true,object_id:req.object_id,payload_sha256:persisted.payload_sha256,
    file_id:file.getId(),registry_range:range};
  if(req.action==='get')result.payload_json=persisted.payload_json;
  return result;
}
function doPost(e) {
  const props=PropertiesService.getScriptProperties(),secret=props.getProperty('ARCHIVE_HMAC_SECRET');
  // Never echo an unsigned request, private payload or secret.
  if(!secret || secret.length<32) return ContentService.createTextOutput('{"error":"NOT_CONFIGURED"}');
  let nonce='';const lock=LockService.getScriptLock();let held=false;
  try {
    archiveAssert(e && e.postData && typeof e.postData.contents==='string','REQUEST_INVALID');
    const env=JSON.parse(e.postData.contents);
    archiveAssert(typeof env.message==='string' && env.message.length<=4200000,'REQUEST_TOO_LARGE');
    archiveAssert(archiveEqual(env.signature,archiveSign(env.message,secret)),'AUTHENTICATION_FAILED');
    const req=JSON.parse(env.message);nonce=req.nonce;
    archiveAssert(typeof nonce==='string' && /^[0-9a-f]{32}$/.test(nonce),'NONCE_INVALID');
    archiveAssert(Number.isInteger(req.timestamp) && Math.abs(Date.now()/1000-req.timestamp)<=300,'REQUEST_EXPIRED');
    lock.waitLock(20000);held=true;
    const cache=CacheService.getScriptCache();archiveAssert(!cache.get('nonce:'+nonce),'REQUEST_REPLAY');
    cache.put('nonce:'+nonce,'1',600);
    return archiveReply(archiveHandle(req,props),nonce,secret);
  }catch(err) {
    const allowed=['REQUEST_INVALID','REQUEST_TOO_LARGE','AUTHENTICATION_FAILED','NONCE_INVALID','REQUEST_EXPIRED',
      'REQUEST_REPLAY','REGISTRO_NOT_FOUND','REGISTRY_SCHEMA_MISMATCH','OBJECT_ID_INVALID','KIND_OR_RUN_INVALID',
      'PAYLOAD_INVALID','PAYLOAD_TOO_LARGE','PAYLOAD_HASH_INVALID','OBJECT_HASH_INVALID','IMMUTABLE_OBJECT_CONFLICT',
      'STORED_HASH_INVALID','DRIVE_READBACK_FAILED','REGISTRY_INDEX_CONFLICT','REGISTRY_WRITE_READBACK_FAILED',
      'LEARNING_HEAD_CONFLICT','LEARNING_BOOTSTRAP_REQUIRED','LEARNING_BOOTSTRAP_AMBIGUOUS','CACHE_HEAD_CONFLICT','DUPLICATE_REGISTRY_INDEX','DUPLICATE_ARCHIVE_OBJECT','REGISTRY_CAPACITY_REVIEW_REQUIRED','ARCHIVE_OBJECT_NOT_FOUND'];
    return archiveReply({ok:false,error:allowed.includes(err.message)?err.message:'GATEWAY_FAILED'},nonce,secret);
  }finally {if(held){SpreadsheetApp.flush();lock.releaseLock();}}
}
