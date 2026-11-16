import json
import pytest
import httpx
from fastapi.testclient import TestClient
from office.config import Settings, Repository, load_repositories
from office.index import Index, allowed
from office.agents import Office
from office.app import create_app
from office.gitlab import GitLab

@pytest.fixture
def settings(tmp_path):
    catalog = tmp_path / 'repositories.yaml'
    catalog.write_text('repositories:\n  - id: payments\n    project: team/payments\n    description: Payments API\n  - id: identity\n    project: team/identity\n')
    return Settings(_env_file=None, data_dir=tmp_path/'data',repositories_file=catalog,
                    gitlab_token='private-gitlab-token',luna_api_key='private-luna-token')

@pytest.fixture
def source(monkeypatch):
    class FakeGitLab:
        texts = {'README.md':'Payment service handles refunds', 'app.py':'from identity import verify\n\ndef refund(payment_id):\n    return verify(payment_id)', '.env':'SECRET=hidden','node_modules/a.js':'ignored'}
        fail = False
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def commit(self,repo): return 'a'*40
        def files(self,repo,sha): return iter(self.texts)
        def read(self,repo,sha,path):
            if self.fail: raise RuntimeError('upstream unavailable')
            return self.texts[path]
    monkeypatch.setattr('office.index.GitLab',FakeGitLab)
    return FakeGitLab

def test_index_search_provenance_and_atomic_failure(settings,source):
    index = Index(settings)
    repo = load_repositories(settings)[0]
    result = index.sync(settings,repo)
    assert result['files']==2 and result['skipped']==2
    hits=index.search('refund',['payments'])
    assert hits and hits[0]['sha']=='a'*40
    assert any(h['path']=='app.py' and h['start']==1 for h in hits)
    assert not index.search('refund',['identity'])
    graph=index.graph([repo])
    assert any('refund' in f['symbols'] for f in graph['files'])
    source.fail=True
    with pytest.raises(RuntimeError): index.sync(settings,repo)
    assert index.search('refund',['payments'])==hits

def test_updated_snapshot_removes_deleted_files(settings,source):
    index=Index(settings); repo=load_repositories(settings)[0]
    index.sync(settings,repo)
    source.texts={'README.md':'replacement only'}
    index.sync(settings,repo)
    assert not index.search('refund',['payments'])
    assert len(index.graph([repo])['files'])==1

def test_allowlist_and_sources(settings,source,monkeypatch):
    index=Index(settings); repos=load_repositories(settings);index.sync(settings,repos[0])
    office=Office(settings,index,repos)
    replies=iter([json.dumps({'repositories':['payments','not-allowed'],'queries':['refund']}),'İade doğrulama çağırır [1].'])
    monkeypatch.setattr(office.luna,'ask',lambda *a:next(replies))
    result=office.run('İade akışı nasıl çalışıyor?')
    assert result['repositories']==['payments']
    assert result['sources'] and all(s['repo']=='payments' for s in result['sources'])

def test_no_index_does_not_call_model(settings,monkeypatch):
    office=Office(settings,Index(settings),load_repositories(settings))
    monkeypatch.setattr(office.luna,'ask',lambda *a:pytest.fail('No LLM request expected'))
    assert office.run('refund')['sources']==[]

def test_api_secrets_and_origin(settings):
    client=TestClient(create_app(settings))
    response=client.get('/api/status')
    assert response.status_code==200
    assert 'private-gitlab-token' not in response.text and 'private-luna-token' not in response.text
    assert client.post('/api/chat',json={'question':' '}).status_code==422
    assert client.post('/api/chat',json={'question':'refund'},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.get('/api/status',headers={'Host':'evil.test'}).status_code==403
    assert client.post('/api/index',json={'repository':'not-allowed'}).status_code==404
    assert client.get('/').status_code==200

def test_repository_validation(settings):
    settings.repositories_file.write_text('repositories:\n - id: a\n   project: x\n   depends_on: [missing]\n')
    with pytest.raises(ValueError):load_repositories(settings)

def test_gitlab_pagination_and_pinned_reads(settings):
    calls=[]
    def handler(request):
        calls.append(request)
        if '/commits/' in request.url.path:return httpx.Response(200,json={'id':'commit-sha'})
        if '/tree' in request.url.path:
            page=request.url.params['page']
            return httpx.Response(200,json=[{'type':'blob','path':'a.py' if page=='1' else 'b.py'}],headers={'x-next-page':'2' if page=='1' else ''})
        return httpx.Response(200,text='def run(): pass')
    with GitLab(settings) as gitlab:
        gitlab.client.close()
        gitlab.client=httpx.Client(base_url=settings.gitlab_url+'/api/v4/',transport=httpx.MockTransport(handler))
        repo=load_repositories(settings)[0]
        sha=gitlab.commit(repo)
        assert list(gitlab.files(repo,sha))==['a.py','b.py']
        assert gitlab.read(repo,sha,'a.py')=='def run(): pass'
        assert all(r.url.params['ref']==sha for r in calls[1:])

def test_sensitive_and_generated_files_are_excluded():
    repo=Repository(id='x',project='x',exclude=['private/**'])
    for path in ['.env','.env.production','credentials.json','vendor/a.py','private/a.py','cert.pem','package-lock.lock']:
        assert not allowed(path,repo)
    assert allowed('src/main.py',repo)
