"""FastAPI: готовность после загрузки и прогрева, неизменная загруженная версия."""
import argparse
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import release


class StubBackend:
    def __init__(self, manifest):
        if manifest.get('kind') in ('base', 'adapter'):
            raise ValueError('Для manifest реальной модели нужен backend real')
        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.calls = 0

    def warmup(self):
        self.generate('Прогрев', 32)

    def generate(self, prompt, limit):
        self.calls += 1
        time.sleep(.01)
        return {'answer': f'STUB ONLY: {self.loaded_id}', 'output_tokens': 1}


class ServiceState:
    def __init__(self, root, warmup_seconds=0, backend_factory=None):
        self.root = Path(root)
        self.warmup_seconds = warmup_seconds
        self.backend_factory = backend_factory or StubBackend
        self.ready = False
        self.backend = None
        self.manifest = None
        self.error = None
        self.warmup_finished = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        self.thread = threading.Thread(target=self._load, daemon=True)
        self.thread.start()

    def _load(self):
        try:
            manifest = json.loads((self.root / 'accepted.json').read_text(encoding='utf-8'))
            release.verify_files(manifest, self.root)
            if release.sha256(release.canonical(manifest['runtime'])) != manifest['runtime_sha256']:
                raise ValueError('Изменён runtime принятой версии')
            self.manifest = manifest
            self.backend = self.backend_factory(manifest)
            time.sleep(self.warmup_seconds)
            self.backend.warmup()
            self.warmup_finished.set()
            self.ready = True
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            print('LOAD_FAILED', self.error, flush=True)

    def generate(self, request):
        if not self.ready:
            raise NotReady('Модель ещё не загружена и не прогрета')
        started = time.perf_counter()
        prompt = request['prompt']
        limit = request['max_new_tokens']
        if self.manifest.get('kind') in ('base', 'adapter'):
            if limit != self.manifest['runtime']['generation']['max_new_tokens']:
                raise ValueError('Для реальной модели допустим только лимит из протокола оценки')
        with self.lock:
            acquired = time.perf_counter()
            result = self.backend.generate(prompt, limit)
            finished = time.perf_counter()
        result.update(text=result['answer'], model_id=self.backend.loaded_id,
                      runtime_sha256=self.backend.runtime_sha256, request_id=str(uuid.uuid4()),
                      queue_seconds=acquired-started, generation_seconds=finished-acquired,
                      total_seconds=finished-started)
        return result


class NotReady(Exception):
    pass


def create_app(workspace, warmup_seconds=0, backend_factory=None, debug=True):
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

    class GenerateRequest(BaseModel):
        model_config = ConfigDict(extra='forbid')
        prompt: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
        max_new_tokens: Annotated[int, Field(strict=True, ge=1, le=32)] = 32

        @field_validator('prompt')
        @classmethod
        def nonblank(cls, value):
            if not value.strip():
                raise ValueError('Пустой prompt')
            return value

    state = ServiceState(workspace, warmup_seconds, backend_factory)

    @asynccontextmanager
    async def lifespan(app):
        state.start()
        yield
        state.ready = False

    app = FastAPI(title='Локальный API классификации тональности', lifespan=lifespan)
    app.state.service = state

    @app.get('/health')
    def health():
        return {'alive': True}

    @app.get('/ready')
    def ready():
        if not state.ready:
            raise HTTPException(503, 'Модель ещё не готова' if not state.error else state.error)
        return {'ready': True, 'model_id': state.backend.loaded_id,
                'runtime_sha256': state.backend.runtime_sha256}

    if debug:
        @app.get('/debug')
        def diagnostic():
            return {'loaded_id': getattr(state.backend, 'loaded_id', None),
                    'calls': getattr(state.backend, 'calls', 0),
                    'warmup_finished': state.warmup_finished.is_set()}

    @app.post('/generate')
    def generate(request: GenerateRequest):
        try:
            return state.generate(request.model_dump())
        except NotReady as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--warmup-seconds', type=float, default=5)
    parser.add_argument('--backend', choices=['stub', 'real'], default='stub')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    factory = None
    if args.backend == 'real':
        from backend import TransformersBackend
        factory = TransformersBackend
    print(f'{args.backend}: http://127.0.0.1:{args.port}', flush=True)
    uvicorn.run(create_app(args.workspace, args.warmup_seconds, factory,
                           debug=args.debug or args.backend == 'stub'),
                host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
