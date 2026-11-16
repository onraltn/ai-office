"""Persistent lexical code retrieval. Each repo snapshot is replaced atomically."""
from contextlib import contextmanager
import fnmatch
import json
import re
import sqlite3
from pathlib import PurePosixPath
from datetime import datetime, timezone
from .config import Settings, Repository
from .gitlab import GitLab

EXTENSIONS = {'.py','.js','.jsx','.ts','.tsx','.java','.kt','.go','.rs','.cs','.rb','.php','.sql','.md','.rst','.yaml','.yml','.toml','.json','.xml','.html','.css','.sh','.tf','.proto','.graphql','.vue','.svelte','.c','.cpp','.h','.properties','.gradle'}
SKIP_PARTS = {'node_modules','vendor','.git','dist','build','.venv','venv','__pycache__','.next','coverage'}
SECRET_NAMES = {'.env','credentials.json','id_rsa','id_ed25519','.npmrc','.pypirc'}

def allowed(path, repo):
    p = PurePosixPath(path)
    return (not set(p.parts) & SKIP_PARTS and p.name not in SECRET_NAMES
            and not p.name.startswith('.env') and p.suffix.lower() in EXTENSIONS
            and not any(fnmatch.fnmatch(path, pattern) for pattern in repo.exclude)
            and not p.name.endswith(('.lock', '.min.js', '.map')))

def symbols(text):
    return re.findall(r'^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|interface|struct|enum|func)\s+(\w+)', text, re.M)

def imports(text):
    return sorted(set(re.findall(r'(?:from\s+|import\s+|require\([\'"])([\w./@-]+)', text)))[:100]

class Index:
    def __init__(self, settings: Settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / 'index.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS repos(id TEXT PRIMARY KEY, sha TEXT, indexed_at TEXT, files INTEGER, skipped INTEGER, overview TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(repo UNINDEXED,path,sha UNINDEXED,start UNINDEXED,end UNINDEXED,content, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS files(repo TEXT,path TEXT,symbols TEXT,imports TEXT, PRIMARY KEY(repo,path));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self):
        with self.connect() as db:
            return {r['id']: dict(r) for r in db.execute('SELECT * FROM repos')}

    def sync(self, settings: Settings, repo: Repository):
        with GitLab(settings) as source:
            sha = source.commit(repo)
            # Reindex even unchanged commits so updated exclusions/metadata take effect.
            chunks, files, skipped, readmes = [], [], 0, []
            total_bytes = 0
            for path in source.files(repo, sha):
                if not allowed(path, repo):
                    skipped += 1
                    continue
                text = source.read(repo, sha, path)
                if text is None:
                    skipped += 1
                    continue
                total_bytes += len(text.encode("utf-8"))
                if total_bytes > 64_000_000:
                    raise ValueError("Repository text exceeds 64 MB; narrow exclusions")
                lines = text.splitlines()
                if PurePosixPath(path).name.lower().startswith('readme'):
                    readmes.append(text[:6000])
                files.append((repo.id,path,json.dumps(symbols(text)),json.dumps(imports(text))))
                for start in range(0,len(lines),60):
                    chunks.append((repo.id,path,sha,start+1,min(start+80,len(lines)),'\n'.join(lines[start:start+80])[:16000]))
            overview = '\n'.join([repo.description, ' '.join(repo.tags), *readmes])[:14000]
            with self.connect() as db:
                db.execute('DELETE FROM chunks WHERE repo=?',(repo.id,))
                db.execute('DELETE FROM files WHERE repo=?',(repo.id,))
                db.executemany('INSERT INTO chunks(repo,path,sha,start,end,content) VALUES(?,?,?,?,?,?)', chunks)
                db.executemany('INSERT INTO files VALUES(?,?,?,?)',files)
                db.execute('INSERT OR REPLACE INTO repos VALUES(?,?,?,?,?,?)',
                           (repo.id,sha,datetime.now(timezone.utc).isoformat(),len(files),skipped,overview))
        return {'repo':repo.id,'sha':sha,'files':len(files),'chunks':len(chunks),'skipped':skipped}

    def search(self, query: str, repo_ids: list[str], limit=12):
        words = re.findall(r'[^\W_]+',query, re.UNICODE)[:40]
        if not words or not repo_ids:
            return []
        expression = ' OR '.join('"'+w+'"' for w in words)
        placeholders = ','.join('?' for _ in repo_ids)
        with self.connect() as db:
            rows = db.execute(f'''SELECT repo,path,sha,start,end,content,bm25(chunks,0,3,0,0,0,1) AS rank
                FROM chunks WHERE chunks MATCH ? AND repo IN ({placeholders}) ORDER BY rank LIMIT ?''',
                              [expression,*repo_ids,limit]).fetchall()
        return [dict(r) for r in rows]

    def graph(self, repos):
        ids = [r.id for r in repos]
        nodes = [{'id':r.id,'description':r.description,'tags':r.tags} for r in repos]
        edges = [{'source':r.id,'target':d,'kind':'declared'} for r in repos for d in r.depends_on]
        files = []
        with self.connect() as db:
            for rid in ids:
                files.extend(dict(r) for r in db.execute('SELECT * FROM files WHERE repo=?',(rid,)))
        for f in files:
            f['symbols'],f['imports'] = json.loads(f['symbols']),json.loads(f['imports'])
        return {'repositories':nodes,'edges':edges,'files':files,
                'note':'Imports are heuristic, not resolved call edges. Cross-repository edges come from depends_on.'}
