/** Version 1.0.0 — private immutable archive + index in existing Registro.
 * Script Properties: ARCHIVE_HMAC_SECRET, ARCHIVE_FOLDER_ID, REGISTRY_ID.
 * No broker APIs, orders, risk calculations or changes to existing records.
 */
const ARCHIVE_VERSION = '1.0.0';
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
function archiveHandle(req,props) {
  archiveAssert(req.protocol===1 && ['status','put','get'].includes(req.action),'REQUEST_INVALID');
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
      'DUPLICATE_REGISTRY_INDEX','DUPLICATE_ARCHIVE_OBJECT','REGISTRY_CAPACITY_REVIEW_REQUIRED','ARCHIVE_OBJECT_NOT_FOUND'];
    return archiveReply({ok:false,error:allowed.includes(err.message)?err.message:'GATEWAY_FAILED'},nonce,secret);
  }finally {if(held){SpreadsheetApp.flush();lock.releaseLock();}}
}
