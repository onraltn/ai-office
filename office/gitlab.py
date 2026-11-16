from urllib.parse import quote
import httpx
from .config import Settings, Repository

MAX_FILE_BYTES = 300_000
MAX_FILES = 5000

class GitLab:
    def __init__(self, settings: Settings):
        self.client = httpx.Client(
            base_url=settings.gitlab_url + '/api/v4/',
            headers={'PRIVATE-TOKEN': settings.gitlab_token.get_secret_value()},
            timeout=45, follow_redirects=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def commit(self, repo: Repository) -> str:
        response = self.client.get(f'projects/{quote(repo.project, safe="")}/repository/commits/{quote(repo.ref, safe="")}')
        response.raise_for_status()
        return response.json()['id']

    def files(self, repo: Repository, sha: str):
        page = 1
        total = 0
        while True:
            response = self.client.get(f'projects/{quote(repo.project, safe="")}/repository/tree',
                                       params={'ref': sha, 'recursive': 'true', 'per_page': 100, 'page': page})
            response.raise_for_status()
            entries = response.json()
            for entry in entries:
                if entry['type'] == 'blob':
                    total += 1
                    if total > MAX_FILES:
                        raise ValueError('Repository exceeds 5000 files; narrow the repository scope before indexing')
                    yield entry['path']
            if not response.headers.get('x-next-page'):
                break
            page = int(response.headers['x-next-page'])

    def read(self, repo: Repository, sha: str, path: str) -> str | None:
        url = f'projects/{quote(repo.project, safe="")}/repository/files/{quote(path, safe="")}/raw'
        with self.client.stream('GET', url, params={'ref': sha}) as response:
            response.raise_for_status()
            body = bytearray()
            for part in response.iter_bytes():
                body.extend(part)
                if len(body) > MAX_FILE_BYTES:
                    return None
        if b'\0' in body:
            return None
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError:
            return None
