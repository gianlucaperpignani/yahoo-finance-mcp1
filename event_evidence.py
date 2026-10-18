"""1.0.0: provenance gate for capped candidate catalyst evidence.

Collector fetches public URLs only from an explicitly verified domain registry.
HTTP and a primary domain do not establish the truth of a catalyst by themselves.
"""
import hashlib
import ipaddress
import json
import socket
from datetime import datetime,timezone
from urllib.parse import urlparse
from urllib.request import Request,urlopen,HTTPRedirectHandler,build_opener


def public_url(url, allowed_hosts):
    p=urlparse(url)
    if p.scheme!="https" or p.hostname not in allowed_hosts or p.username or p.password or p.port not in (None,443): raise ValueError("SOURCE_URL_NOT_ALLOWED")
    for address in socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[4][0]).is_global: raise ValueError("NON_PUBLIC_SOURCE")
    return p.hostname


class CheckedRedirect(HTTPRedirectHandler):
    def __init__(self,hosts): self.hosts=hosts
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        public_url(newurl,self.hosts)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def collect(urls,registry):
    verified={r["host"]:r for r in registry if r.get("verified_at") and r.get("verification_reference") and r.get("issuer_id")}
    records=[]
    for url in list(dict.fromkeys(urls))[:3]:
        record=dict(url=url,retrieved_at=datetime.now(timezone.utc).isoformat(),status="MISSING")
        try:
            host=public_url(url,verified)
            with build_opener(CheckedRedirect(set(verified))).open(Request(url,headers={"User-Agent":"eToroResearchAudit/1.0","Accept":"text/html,application/json,text/plain"}),timeout=10) as response:
                content=response.read(1000001)
                if len(content)>1000000: raise ValueError("DOCUMENT_TOO_LARGE")
                record.update(status="RETRIEVED",http_status=response.status,final_url=response.url,content_sha256=hashlib.sha256(content).hexdigest(),text=content.decode("utf-8",errors="replace"))
                record["registry"]=verified[urlparse(response.url).hostname]
        except Exception as exc: record["reason"]=str(exc)[:180]
        records.append(record)
    return records


def verify(event):
    docs=[]
    for d in event.get("documents",[]):
        r=d.get("registry",{});text=d.get("text");facts=event.get("required_facts",[])
        if d.get("status")!="RETRIEVED" or not isinstance(text,str) or hashlib.sha256(text.encode()).hexdigest()!=d.get("content_sha256"):
            # For non-UTF8 originals retain raw-byte SHA separately, do not certify a re-encoding.
            continue
        if urlparse(d.get("url", "")).hostname!=r.get("host") or not r.get("verified_at"): continue
        if r.get("issuer_id")!=event.get("issuer_id") or not r.get("verification_reference") or not facts or not all(isinstance(f,str) and f and f.casefold() in text.casefold() for f in facts): continue
        if d.get("fact_review",{}).get("confirmed") is not True or not d["fact_review"].get("reference"): continue
        docs.append(d)
    primary=[d for d in docs if d["registry"].get("kind") in {"ISSUER_IR","REGULATOR","EXCHANGE"}]
    independent={d.get("origin_id") for d in docs if d["registry"].get("kind")=="FINANCIAL_NEWS" and d.get("origin_id") and d.get("origin_evidence_reference")}
    news=[d for d in docs if d["registry"].get("kind")=="FINANCIAL_NEWS"]
    passed=bool(primary) or (len(independent)>=2 and len({urlparse(d["url"]).hostname for d in news})>=2 and len({d["content_sha256"] for d in news})>=2)
    if event.get("category")=="M&A": passed=bool(primary) and event.get("official_announcement_confirmed") is True
    return dict(version="1.0.0",state="PASS" if passed else "MISSING",documents_considered=len(docs),
                reason="verified primary or two independent origins" if passed else "primary/independent provenance or material fact confirmation missing",
                source_references=[d["url"] for d in docs],ticket_authorized=False)
