"""1.0.0: technical precollection at approved full-scan slots via existing /warm.

No ChatGPT calls, no account/orders, no learned-policy modification. The normal
agent still validates state, portfolio, risk and tickets at each planned slot.
"""
import copy
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from archive_client import ArchiveClient,ArchiveError,configuration_status

VERSION='1.0.0'
SLOTS={'09:20':['EU'],'16:00':['US']}


def due(now):
    now=now.astimezone(ZoneInfo('Europe/Rome'))
    if now.weekday()>=5:return None
    minute=now.hour*60+now.minute
    for slot,markets in SLOTS.items():
        h,m=map(int,slot.split(':'));delta=minute-(h*60+m)
        if 0<=delta<15:return now.date().isoformat()+'|'+slot,markets
    return None


class Collector:
    def __init__(self,factory=ArchiveClient.from_env,clock=None):
        self.factory=factory;self.clock=clock or (lambda:datetime.now(ZoneInfo('Europe/Rome')))
        self.lock=threading.Lock();self.busy=False;self.attempted={};self.info=dict(version=VERSION,state='IDLE',technical_only=True,account_collected=False)
    def status(self):
        with self.lock:return dict(copy.deepcopy(self.info),configured=configuration_status()['configured'],slots=list(SLOTS))
    def tick(self,scan):
        if not configuration_status()['configured']:return
        with self.lock:self.info['last_warm_call']=self.clock().isoformat()
        slot=due(self.clock())
        if not slot:return
        key,markets=slot
        with self.lock:
            if self.busy or time.monotonic()-self.attempted.get(key,0)<60:return
            self.busy=True;self.attempted={key:time.monotonic()}
            self.info.update(state='CLAIMING',slot=key)
        threading.Thread(target=self._work,args=(key,markets,scan),daemon=True,name='cron-collector').start()
    def _work(self,key,markets,scan):
        try:
            client=self.factory();claim=client._call('collector_claim',slot_key=key)
            if not claim.get('claimed'):
                with self.lock:
                    self.attempted[key]=time.monotonic()+86400
                    self.info.update(state='ALREADY_CLAIMED',slot=key)
                return
            # Calls the same asynchronous job API as ChatGPT; no ticker output.
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                result=scan(markets)
                with self.lock:self.info.update(state='SCANNING',job_id=result.get('job_id'))
                if result.get('job_status')=='COMPLETE':
                    receipt=result.get('opportunity_archive',{}).get('persistence',{})
                    if receipt.get('durable_verified'):
                        client._call('collector_finish',slot_key=key,claim_token=claim['claim_token'],object_id=receipt['object_id'])
                        with self.lock:
                            self.attempted[key]=time.monotonic()+86400
                            self.info.update(state='ARCHIVED',archive_object_id=receipt['object_id'],scan_complete=result.get('scan_complete'),returned=result.get('returned'),ticket_authorized=False)
                        return
                elif result.get('job_status')=='ERROR':raise ArchiveError('CRON_SCANNER_JOB_FAILED')
                time.sleep(30)
            raise ArchiveError('CRON_COLLECTOR_TIME_BUDGET')
        except ArchiveError as exc:
            with self.lock:self.info.update(state='ERROR',error=str(exc))
        except Exception:
            with self.lock:self.info.update(state='ERROR',error='CRON_COLLECTOR_FAILED')
        finally:
            with self.lock:self.busy=False


runtime=Collector()


def _after_fork():
    runtime.lock=threading.Lock();runtime.busy=False;runtime.attempted={}
    runtime.info=dict(version=VERSION,state='IDLE',technical_only=True,account_collected=False)

import os
if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_child=_after_fork)
