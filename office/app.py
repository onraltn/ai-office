import threading
import uuid
from urllib.parse import urlparse, quote
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field
import httpx
from .config import Settings, load_repositories
from .index import Index
from .agents import Office

ROLES = [('alfred','Alfred','CEO',True),('developer','Developer','Kod araştırması',True),
         ('analyst-qa','Analyst & QA','Analiz ve kalite',False),('security','Security','Güvenlik',False),
         ('architect','Architect','Mimari',False),('devops','DevOps','Operasyon',False),('db','DB Agent','Veri tabanı',False)]

class Question(BaseModel):
    question: str = Field(min_length=1,max_length=4000)

class Sync(BaseModel):
    repository: str | None = None

def create_app(settings: Settings | None = None):
    from dotenv import load_dotenv
    load_dotenv(override=False)
    settings = settings or Settings()
    index = Index(settings)
    app = FastAPI(title='AI Office', docs_url='/api/docs', redoc_url=None)
    jobs = {}
    lock = threading.Lock()
    static = Path(__file__).parent / 'static'

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = urlparse('http://' + request.headers.get('host','')).hostname
        origin = request.headers.get('origin')
        if host not in {'localhost','127.0.0.1','::1','testserver'}:
            return JSONResponse({'detail':'Local access only'},status_code=403)
        if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail':'Origin rejected'},status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    def repos():
        try:
            return load_repositories(settings)
        except (ValueError,OSError) as exc:
            raise HTTPException(422,'repositories.yaml could not be loaded or validated') from exc

    @app.get('/')
    def home():
        return FileResponse(static / 'index.html')

    @app.get('/api/status')
    def status():
        snapshots = index.status()
        return {'agents':[{'id':i,'name':n,'role':r,'enabled':e} for i,n,r,e in ROLES],
                'repositories':[{'id':r.id,'project':r.project,'ref':r.ref,'description':r.description,
                                 'tags':r.tags,'index':snapshots.get(r.id)} for r in repos()],
                'connections':{'gitlab_url':settings.gitlab_url,'gitlab_configured':bool(settings.gitlab_token.get_secret_value()),
                               'luna_base_url':settings.luna_base_url,'luna_model':settings.luna_model,
                               'luna_configured':bool(settings.luna_api_key.get_secret_value())},
                'jobs':list(jobs.values())[-20:]}

    @app.get('/api/graph')
    def graph():
        return index.graph(repos())

    @app.post('/api/index',status_code=202)
    def sync(body: Sync):
        selected = [r for r in repos() if body.repository is None or r.id == body.repository]
        if not selected:
            raise HTTPException(404,'No configured repository selected')
        if not settings.gitlab_token.get_secret_value():
            raise HTTPException(422,'GITLAB_TOKEN is not configured')
        if not lock.acquire(blocking=False):
            raise HTTPException(409,'Indexing is already running')
        jid = uuid.uuid4().hex
        job = {'id':jid,'state':'running','results':[], 'error':None}
        jobs[jid] = job
        def work():
            try:
                for repo in selected:
                    job['results'].append(index.sync(settings,repo))
                job['state'] = 'completed'
            except Exception:
                # Do not expose upstream responses, URLs or secrets to browsers/logs.
                job['state'],job['error'] = 'failed','Indexing failed. Check GitLab access, project/ref and repository limits; prior snapshots remain available.'
            finally:
                lock.release()
        threading.Thread(target=work,daemon=True).start()
        return {'job_id':jid}

    @app.post('/api/chat')
    def chat(body: Question):
        if not body.question.strip():
            raise HTTPException(422,'Question must not be blank')
        configured = repos()
        try:
            result = Office(settings,index,configured).run(body.question)
        except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
            raise HTTPException(502,'Luna request failed. Check endpoint, model, token and OpenAI-compatible API support.') from exc
        by_id = {r.id:r for r in configured}
        for source in result['sources']:
            repo = by_id[source['repo']]
            # Numeric project IDs do not provide a reliable browser project path.
            source['url'] = (settings.gitlab_url + '/' + quote(repo.project,safe='/') + '/-/blob/' +
                             source['sha'] + '/' + quote(source['path'],safe='/') + f'#L{source["start"]}-{source["end"]}') if '/' in repo.project else None
        return result

    app.mount('/static', StaticFiles(directory=static),name='static')
    return app
