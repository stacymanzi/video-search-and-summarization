"""Regression scenarios from the November 17 Brev notebook audit; no live writes."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import course_kafka as kafka
import course_detections as detections
import cleanup


class Frame:
    def ParseFromString(self, payload):
        self.sensorId = payload.decode()


def kafka_fake(partitions, *, first=17, fail=False, truncated=False):
    reads = []
    def run(args, **kwargs):
        if 'kafka-get-offsets' in args:
            early = args[args.index('--time') + 1] == '-2'
            raw = '\n'.join(f'mdx-raw:{p}:{first if early else first + len(rows)}'
                            for p, rows in partitions.items()).encode()
            return SimpleNamespace(returncode=0, stdout=raw, stderr=b'')
        p = int(args[args.index('--partition') + 1])
        offset = int(args[args.index('--offset') + 1])
        count = int(args[args.index('--max-messages') + 1])
        marker = args[-1].split('=', 1)[1].encode()
        rows = partitions[p][offset-first:offset-first+count]
        reads.append((p, offset, count))
        raw = b''.join(row.encode()+marker for row in rows)
        return SimpleNamespace(returncode=1 if fail else 0,
                               stdout=raw[:-1] if truncated else raw, stderr=b'consumer failed')
    return run, reads


def test_kafka_finds_older_source_with_other_producers_and_nonzero_retention():
    run, reads = kafka_fake({0:['rain_check']*2+['Camera_1']*7, 1:['fog_eval']*9})
    with patch.object(kafka.subprocess, 'run', side_effect=run):
        payloads = kafka.source_payloads('rain_check', Frame, batch_size=3)
    assert payloads == [b'rain_check']*2
    assert len(reads) > 2
    assert min(offset for _,offset,_ in reads) == 17


@pytest.mark.parametrize('fail,truncated', [(True,False),(False,True)])
def test_kafka_never_uses_failed_or_partial_subprocess_output(fail,truncated):
    run,_=kafka_fake({0:['rain_check']},fail=fail,truncated=truncated)
    with patch.object(kafka.subprocess,'run',side_effect=run), pytest.raises(RuntimeError):
        kafka.source_payloads('rain_check',Frame)


def test_kafka_budget_and_absent_source_diagnostics():
    run, reads=kafka_fake({0:['Camera_1']*10})
    with patch.object(kafka.subprocess,'run',side_effect=run), pytest.raises(RuntimeError,match='message budget reached.*Camera_1'):
        kafka.source_payloads('rain_check',Frame,max_messages=4,batch_size=3)
    assert sum(count for _,_,count in reads)==4
    run,_=kafka_fake({0:['fog_eval']})
    with patch.object(kafka.subprocess,'run',side_effect=run), pytest.raises(RuntimeError,match='retained snapshot exhausted'):
        kafka.source_payloads('rain_check',Frame)


class Clock:
    now=0
    def monotonic(self): return self.now
    def sleep(self,seconds): self.now+=seconds


def snapshot_mock(counts, totals):
    counts=iter(counts); totals=iter(totals)
    def es(method,path,**kwargs):
        if path.endswith('_count'): return {'count':next(counts)}
        count=next(totals)
        return {'hits':{'total':{'value':count,'relation':'eq'},'hits':[{'_id':str(i)} for i in range(count)]}}
    return es


def test_snapshot_waits_through_partial_plateau_and_fetch_race():
    # First stable count is invalidated by new frames during the fetch.
    clock=Clock()
    api=snapshot_mock([2,2,3,3,3,4,4,4,4], [3,4])
    with patch.object(detections,'time',clock),patch.object(detections,'es',side_effect=api):
        hits=detections.detection_snapshot('index','traffic_short',stable_s=10,every_s=5,timeout_s=60)
    assert len(hits)==4 and clock.now>=30


def test_snapshot_rejects_truncation_and_no_progress():
    with patch.object(detections,'es',return_value={'count':2001}),pytest.raises(RuntimeError,match='exceeds'):
        detections.detection_snapshot('index','clip')
    with patch.object(detections,'time',Clock()),patch.object(detections,'es',return_value={'count':0}),pytest.raises(TimeoutError):
        detections.detection_snapshot('index','clip',timeout_s=15,every_s=5)


def worker(tmp_path):
    ledger=tmp_path/'resources.json'
    cleanup.remember('live','owned',name='Camera_1',stream_id='main',ledger=ledger)
    return cleanup.Cleanup(ledger=ledger)


def test_live_storage_deletes_exact_listed_segments_and_rejects_other_owner(tmp_path):
    w=worker(tmp_path)
    def api(method,path,**kwargs):
        if method=='GET':return {'owned':[{'mediaFilePath':'/recordings/owned/segment.mkv'}]}
        return {'spaceSaved':1}
    with patch.object(w,'api',side_effect=api) as call:
        w.remove_storage(w.resources[0])
    deletes=[c for c in call.call_args_list if c.args[0]=='DELETE']
    assert len(deletes)==1 and deletes[0].kwargs['params']=={'filePath':'/recordings/owned/segment.mkv'}
    with patch.object(w,'api',return_value={'other':[{'mediaFilePath':'/other.mp4'}]}),pytest.raises(RuntimeError,match='ownership'):
        w.remove_storage(w.resources[0])


def test_cleanup_retries_late_segment_and_repeat_run_is_safe(tmp_path):
    w=worker(tmp_path)
    with patch.object(w,'sensors',return_value={}),patch.object(w,'remove_storage') as remove, \
         patch.object(w,'remove_metadata'),patch.object(w,'verify',side_effect=[cleanup.StoragePending(),None,None,None,None]), \
         patch.object(cleanup.time,'sleep'):
        w.run(confirm=True)
        w.run(confirm=True)
    assert remove.call_count==3


def test_cleanup_never_reports_success_with_persistent_storage(tmp_path):
    w=worker(tmp_path)
    with patch.object(w,'sensors',return_value={}),patch.object(w,'remove_storage'), \
         patch.object(w,'remove_metadata'),patch.object(w,'verify',side_effect=cleanup.StoragePending()), \
         patch.object(w,'clear_saved_results') as clear,patch.object(cleanup.time,'sleep'), \
         pytest.raises(RuntimeError,match='did not converge'):
        w.run(confirm=True)
    clear.assert_not_called()


def test_protected_segment_is_not_silently_skipped(tmp_path):
    w=worker(tmp_path)
    with patch.object(w.session,'request') as req:
        req.return_value.json.return_value={'spaceSaved':0,'protectedFiles':['/owned.mkv']}
        req.return_value.content=b'{}'
        with pytest.raises(RuntimeError,match='protectedFiles'):
            w.api('DELETE','/vios/vst/api/v1/storage/file',params={'filePath':'/owned.mkv'})


@pytest.mark.parametrize('has_embedding',[False,True])
def test_example_search_preserves_chosen_vehicle_and_overlay(has_embedding):
    n=json.loads((Path(__file__).parents[1]/'notebooks/02-rt-cv-detection-and-tracking.ipynb').read_text())
    code=''.join(n['cells'][80]['source'])
    wrong={'id':'1','embedding':{'vector':[1]}}
    chosen={'id':'7','embedding':{'vector':[7]}}
    objects=[wrong, chosen if has_embedding else {'id':'7'}]
    es=Mock(return_value={'hits':{'hits':[{'_source':{'timestamp':'later','objects':[wrong,chosen]}}]}})
    env=dict(TRACKLET='7',objects=objects,iso=lambda s:'overlay',SECONDS=5,es=es,INDEX='i',SOURCE='traffic_short')
    exec(code,env)
    assert env['TRACKLET']=='7' and env['objects'] is objects and env['example']==chosen
    if has_embedding: es.assert_not_called()
    else:
        q=es.call_args.kwargs['json']['query']['bool']['filter'][1]['nested']['query']['bool']['filter']
        assert {'term':{'objects.id.keyword':'7'}} in q


def test_live_cleanup_end_to_end_preserves_other_video_and_metadata(tmp_path):
    w=worker(tmp_path)
    cleanup.remember('nvstreamer','nv-owned',name='live_traffic',stream_id='nv-main',ledger=w.ledger)
    w=cleanup.Cleanup(ledger=w.ledger)
    files={'owned':['/recordings/owned/live.mkv'], 'traffic':['/recordings/traffic_short.mp4']}
    metadata={'Camera_1':3, 'traffic_short':382, 'rain_eval':4320}
    nv={'nv-owned':{'sensorId':'nv-owned','state':'online'}}
    deletes=[]
    def api(method,path,**kwargs):
        if path=='/vios/vst/api/v1/sensor/list':
            return [{'sensorId':'owned','name':'Camera_1','state':'removed'},
                    {'sensorId':'traffic','name':'traffic_short','state':'online'}]
        if path=='/nvstreamer/api/v1/sensor/list': return list(nv.values())
        if path=='/nvstreamer/api/v1/storage/file/nv-main':
            nv.clear(); return {'spaceSaved':1}
        if path=='/vios/vst/api/v1/storage/timelines':
            return {key:[{'startTime':'a','endTime':'b'}] for key,val in files.items() if val}
        if path.endswith('/file/owned/list') or path.endswith('/file/main/list'):
            return {'owned':[{'mediaFilePath':p} for p in files['owned']]}
        if path=='/vios/vst/api/v1/storage/file':
            target=kwargs['params']['filePath'];deletes.append(target)
            assert target=='/recordings/owned/live.mkv'
            if len(deletes)>1: files['owned']=[]  # asynchronous first delete
            return {'spaceSaved':1}
        if path=='/rtvi-vlm/v1/files': return {'data':[]}
        if path.startswith('/es/_cat/indices/'):
            return [{'index':'mdx-raw-test'}] if 'mdx-raw-' in path else []
        names=kwargs['json']['query']['terms']['sensorId.keyword']
        if path.endswith('/_delete_by_query'):
            for name in names: metadata.pop(name,None)
            return {'failures':[]}
        if path.endswith('/_count'): return {'count':sum(metadata.get(n,0) for n in names)}
        raise AssertionError((method,path,kwargs))
    with patch.object(w,'api',side_effect=api),patch.object(cleanup.time,'sleep'):
        w.run(confirm=True)
        w.run(confirm=True)
    assert files=={'owned':[], 'traffic':['/recordings/traffic_short.mp4']}
    assert metadata=={'traffic_short':382,'rain_eval':4320}
    assert nv=={} and len(deletes)==2


def test_kafka_timeout_is_a_failure_with_context():
    import subprocess
    with patch.object(kafka.subprocess,'run',side_effect=subprocess.TimeoutExpired('docker',1)), \
         pytest.raises(TimeoutError,match="rain_check"):
        kafka.source_payloads('rain_check',Frame)
