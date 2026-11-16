from pathlib import Path
from urllib.parse import urlparse
import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    gitlab_url: str = 'https://gitlab.example.com'
    gitlab_token: SecretStr = SecretStr('')
    luna_base_url: str = 'https://luna.example.com/v1'
    luna_api_key: SecretStr = SecretStr('')
    luna_model: str = 'luna'
    repositories_file: Path = Path('repositories.yaml')
    data_dir: Path = Path('data')

    @field_validator('gitlab_url', 'luna_base_url')
    @classmethod
    def secure_url(cls, value):
        u = urlparse(value)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('Use an HTTPS URL without credentials, query or fragment')
        return value.rstrip('/')

class Repository(BaseModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    project: str
    ref: str = 'main'
    description: str = ''
    tags: list[str] = []
    depends_on: list[str] = []
    exclude: list[str] = []

    @field_validator('project', 'ref')
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or len(value) > 300 or '\n' in value:
            raise ValueError('Invalid project/ref')
        return value

def load_repositories(settings: Settings) -> list[Repository]:
    try:
        doc = yaml.safe_load(settings.repositories_file.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError('Invalid repositories YAML') from exc
    if not isinstance(doc,dict) or not isinstance(doc.get('repositories',[]),list):
        raise ValueError('Expected repositories list')
    repos = [Repository.model_validate(r) for r in doc.get('repositories', [])]
    ids = [r.id for r in repos]
    if len(ids) != len(set(ids)):
        raise ValueError('Repository IDs must be unique')
    for repo in repos:
        if set(repo.depends_on) - set(ids):
            raise ValueError(f'{repo.id}: unknown depends_on repository')
    return repos
