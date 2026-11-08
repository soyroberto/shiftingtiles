import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import Config, Library, PublisherError, manifest, photo_path
from helpers import FakeStore
from library_sync import COORDINATOR, LibraryStore, run_libraries
from storage import validate_state
from sync import commit_publication, recover_publication
from test_resumable import config as engine_config, BatchMemory, Pages, page, row, Clock
import resumable


def item(ident='spotify'):
    return {'path': photo_path(ident,'photo','version','jpg'), 'version':'version'}


class Shared(FakeStore):
    def __init__(self):
        super().__init__(); self.controls={};self.manifests={};self.control_reads=[]
        self.durable.update(owner='owner',connected=True,client_id='client',cache='{}')
        self.public=SimpleNamespace(get_blob_client=lambda name:SimpleNamespace(exists=lambda:name in self.manifests))
        self.memory=BatchMemory();self.uploads=[];self.metadata={}
    def guard(self):pass
    def load_control(self,name='publisher-work.json'):
        self.control_reads.append(name);return copy.deepcopy(self.controls.get(name))
    def save_control(self,data,name='publisher-work.json'):self.controls[name]=copy.deepcopy(data)
    def publish(self,data,name='gallery.json'):
        if self.fail_publish:raise PublisherError('simulated_publish_failure')
        self.manifests[name]=copy.deepcopy(data)
    def public_inventory(self,library_id):
        return {name:{'bytes':len(data),'sha256':self.metadata[name]} for name,data in self.blobs.items()
                if name.startswith('photos/onedrive/'+library_id+'/')}
    def put_chunk(self,*args,**kwargs):return self.memory.put_chunk(*args,**kwargs)
    def read_chunk(self,*args,**kwargs):return self.memory.read_chunk(*args,**kwargs)
    def upload_original(self,path,content,mime,digest):
        ident=path.split('/')[2]
        name='publisher-work.json' if ident=='spotify' else 'libraries/'+ident+'/publisher-work.json'
        control=self.controls[name];intent=self.read_chunk(control['job']['intent_ref'])
        assert path in [rec['path'] for rec in intent['records']]
        assert digest==hashlib.sha256(content).hexdigest()
        self.blobs[path]=content;self.metadata[path]=digest;self.uploads.append(path)


def seed_all(base):
    base.durable['libraries']={ident:{'items':{'photo':item(ident)}} for ident in ('spotify','instagram')}
    base.durable['managed']=[item(ident)['path'] for ident in ('spotify','instagram')]


def cfg(libraries=None,**kw):
    return Config(True,libraries or [Library('spotify','Musica/Spotify',True),Library('instagram','Instagram',True)],'client',**kw)


def test_projected_state_never_contains_other_library_images():
    base=Shared();seed_all(base);view=LibraryStore(base,'instagram');state=view.load()
    assert set(state['libraries'])=={'instagram'}
    assert state['managed']==[item('instagram')['path']]
    assert state['cache']=='{}' and state['owner']=='owner'


def test_auth_save_preserves_other_library_and_transaction():
    base=Shared();seed_all(base)
    spotify_tx={'libraries':{'spotify':base.durable['libraries']['spotify']},'manifest':manifest({'spotify':base.durable['libraries']['spotify']})}
    base.durable['library_transactions']={'spotify':spotify_tx}
    view=LibraryStore(base,'instagram');state=view.load();state['cache']='updated';view.save(state)
    assert base.durable['library_transactions']=={'spotify':spotify_tx}
    assert base.durable['libraries']['spotify']['items']['photo']==item()
    assert base.durable['cache']=='updated'


def test_publication_uses_own_manifest_and_only_spotify_has_root_alias():
    base=Shared();seed_all(base)
    view=LibraryStore(base,'instagram');state=view.load();commit_publication(view,state,state['libraries'])
    assert set(base.manifests)=={'photos/onedrive/instagram/gallery.json'}
    assert base.manifests[view.manifest_name]['images']==[item('instagram')['path']]
    assert base.durable['library_transactions']=={}
    assert set(base.durable['libraries'])=={'spotify','instagram'}
    view=LibraryStore(base,'spotify');state=view.load();commit_publication(view,state,state['libraries'])
    assert base.manifests['gallery.json']['images']==[item()['path']]


def test_failed_publication_keeps_only_selected_transaction_and_recovery_preserves_other_library():
    base=Shared();seed_all(base);view=LibraryStore(base,'instagram');state=view.load();base.fail_publish=True
    with pytest.raises(PublisherError):commit_publication(view,state,state['libraries'])
    assert set(base.durable['library_transactions'])=={'instagram'}
    assert base.durable['transaction'] is None
    base.fail_publish=False;view=LibraryStore(base,'instagram');state=view.load();recover_publication(view,state)
    assert base.durable['library_transactions']=={}
    assert base.durable['libraries']['spotify']['items']['photo']==item()


def test_legacy_spotify_transaction_migrates_without_reset():
    base=Shared();seed_all(base);base.durable['transaction']={'libraries':{'spotify':base.durable['libraries']['spotify']},'manifest':manifest({'spotify':base.durable['libraries']['spotify']})}
    view=LibraryStore(base,'spotify');state=view.load();view.save(state)
    assert base.durable['transaction'] is None and set(base.durable['library_transactions'])=={'spotify'}
    recover_publication(view,state);assert base.durable['library_transactions']=={}
    assert 'instagram' in base.durable['libraries']


@pytest.mark.parametrize('method',['publish','save'])
def test_cross_library_state_and_manifest_are_rejected(method):
    base=Shared();seed_all(base);view=LibraryStore(base,'instagram');state=view.load()
    if method=='publish':
        with pytest.raises(PublisherError,match='cross_library_manifest'):view.publish(manifest(base.durable['libraries']))
    else:
        state['managed'].append(item()['path'])
        with pytest.raises(PublisherError):view.save(state)


def test_owner_change_rejected():
    base=Shared();view=LibraryStore(base,'spotify');state=view.load();state['owner']='other'
    with pytest.raises(PublisherError,match='library_owner_changed'):view.save(state)


def test_backfill_only_committed_library_and_no_empty_placeholder():
    base=Shared();seed_all(base);LibraryStore(base,'spotify').ensure_manifest()
    assert base.manifests['gallery.json']['images']==[item()['path']]
    assert base.manifests['photos/onedrive/spotify/gallery.json']['images']==[item()['path']]
    other=Shared();LibraryStore(other,'instagram').ensure_manifest();assert other.manifests=={}


def test_named_controls_keep_existing_spotify_journal():
    base=Shared()
    assert LibraryStore(base,'spotify').control_name=='publisher-work.json'
    assert LibraryStore(base,'instagram').control_name=='libraries/instagram/publisher-work.json'


def test_hourly_starts_both_then_continuation_rotates_without_starting_idle_library():
    base=Shared();calls=[]
    def engine(view,config,**kw):
        calls.append((view.library_id,kw['start_new'],[lib.id for lib in config.libraries]));return {'status':'ok','images':1}
    run_libraries(base,cfg(),engine=engine,clock=lambda:0)
    run_libraries(base,cfg(),start_new=False,engine=engine,clock=lambda:0)
    assert calls==[('spotify',True,['spotify']),('instagram',True,['instagram'])]
    assert run_libraries(base,cfg(),start_new=False,engine=engine,clock=lambda:0)=={'status':'idle'}
    assert len(calls)==2 and base.controls[COORDINATOR]['pending_starts']==[]


@pytest.mark.parametrize('start_new',[True,False])
def test_new_library_is_not_starved_by_existing_long_spotify_job(start_new):
    base=Shared();memory=BatchMemory();clock=Clock();clock.value=0
    monkey=pytest.MonkeyPatch();monkey.setattr(resumable,'silent_token',lambda *_:'token')
    try:
        graph=Pages([page([row()])]);graph.scan_hook=lambda _:setattr(clock,'value',171)
        resumable.run_resumable(memory,engine_config(),graph_factory=lambda *_args,**_kw:graph,clock=clock)
    finally:monkey.undo()
    base.controls['publisher-work.json']=copy.deepcopy(memory.control)
    seen=[]
    run_libraries(base,cfg(),start_new=start_new,engine=lambda view,*_,**kw:seen.append(view.library_id) or {'status':'ok'},clock=lambda:0)
    assert seen==['instagram']
    run_libraries(base,cfg(),start_new=False,engine=lambda view,*_,**kw:seen.append(view.library_id) or {'status':'deferred'},clock=lambda:0)
    assert seen==['instagram','spotify']


def test_disabled_has_zero_remote_calls_and_disabled_library_is_not_read():
    store=Mock();disabled=Config(False,[],'')
    assert run_libraries(store,disabled)=={'status':'disabled'};assert not store.mock_calls
    base=Shared();run_libraries(base,cfg([Library('spotify','Musica/Spotify',True,False),Library('instagram','Instagram',True)]),engine=lambda *_a,**_kw:{'status':'ok'},clock=lambda:0)
    assert 'publisher-work.json' not in base.control_reads
    assert base.controls[COORDINATOR]['last_library']=='instagram'


@pytest.mark.parametrize('tx',[
    {'libraries':{'instagram':{'items':{'photo':item()}}},'manifest':manifest({'spotify':{'items':{'photo':item()}}})},
    {'libraries':{'spotify':{'items':{'photo':item()}}},'manifest':manifest({'spotify':{'items':{'photo':item()}}})},
])
def test_global_validation_rejects_foreign_library_transaction(tx):
    base=Shared();seed_all(base);base.durable['library_transactions']={'instagram':tx}
    with pytest.raises(PublisherError,match='invalid_private_state'):validate_state(base.durable)


def test_real_engine_keeps_library_manifests_and_controls_separate(monkeypatch):
    base=Shared();monkeypatch.setattr(resumable,'silent_token',lambda store,state,*_:store.save(state) or 'token')
    graphs=[]
    def engine(view,config,**kwargs):
        graph=Pages([page([row()])]);graphs.append(graph)
        return resumable.run_resumable(view,config,graph_factory=lambda *_a,**_k:graph,clock=lambda:0,**kwargs)
    config=cfg([Library('spotify','Musica/Spotify'),Library('instagram','Instagram')],progressive=True)
    assert run_libraries(base,config,engine=engine,clock=lambda:0)['status']=='ok'
    assert run_libraries(base,config,start_new=False,engine=engine,clock=lambda:0)['status']=='ok'
    assert set(base.durable['libraries'])=={'spotify','instagram'}
    for ident in ('spotify','instagram'):
        data=base.manifests['photos/onedrive/'+ident+'/gallery.json']
        assert len(data['images'])==1 and all(p.startswith('photos/onedrive/'+ident+'/') for p in data['images'])
    assert base.manifests['gallery.json']==base.manifests['photos/onedrive/spotify/gallery.json']
    assert base.controls['publisher-work.json']['completed']['runs']==1
    assert base.controls['libraries/instagram/publisher-work.json']['completed']['runs']==1
    assert len(base.uploads)==2


def test_real_engine_recovers_instagram_publication_before_more_downloads(monkeypatch):
    base=Shared();monkeypatch.setattr(resumable,'silent_token',lambda store,state,*_:store.save(state) or 'token')
    graphs=[]
    def engine(view,config,**kwargs):
        graph=Pages([page([row()])]);graphs.append(graph)
        return resumable.run_resumable(view,config,graph_factory=lambda *_a,**_k:graph,clock=lambda:0,**kwargs)
    config=cfg([Library('instagram','Instagram')],progressive=True)
    base.fail_publish=True
    assert run_libraries(base,config,engine=engine,clock=lambda:0)['status']=='failed'
    assert base.durable['library_transactions']['instagram'] is not None
    base.fail_publish=False
    assert run_libraries(base,config,start_new=False,engine=engine,clock=lambda:0)['status']=='ok'
    assert graphs[-1].downloads==[] and len(base.uploads)==1
    assert 'gallery.json' not in base.manifests
    assert base.durable['library_transactions']=={}
