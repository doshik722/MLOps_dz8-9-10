"""Загрузчик закреплённого runtime ДЗ 6+7; модель импортируется только при старте API."""
import json
import importlib.metadata
from pathlib import Path

from release import canonical, sha256, verify_files


TOKENIZER_FILES = ('tokenizer.json', 'tokenizer_config.json', 'vocab.json', 'merges.txt',
                   'special_tokens_map.json', 'added_tokens.json')


def tokenizer_hash(root):
    return sha256(canonical({name: sha256((Path(root) / name).read_bytes())
                             for name in TOKENIZER_FILES if (Path(root) / name).is_file()}))


class TransformersBackend:
    def __init__(self, manifest):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.runtime = manifest['runtime']
        self.calls = 0
        if any(importlib.metadata.version(name) != version for name, version in self.runtime['libraries'].items()):
            raise ValueError('Версии библиотек генератора отличаются от исходной оценки')
        if self.runtime['generation']['do_sample'] is not False:
            raise ValueError('Данный протокол закрепляет greedy-генерацию')
        artifact = verify_files(manifest, Path.cwd())
        base = artifact
        if manifest['kind'] == 'adapter':
            base = verify_files(manifest['base_manifest'], Path.cwd())
        if self.runtime['dtype'] != 'float32' or self.runtime['device'] != 'cpu':
            raise ValueError('Данный воспроизводимый стенд закреплён на CPU/float32')
        if json.loads((Path(base) / 'config.json').read_text(encoding='utf-8')).get('model_type') != 'qwen3':
            raise ValueError('Артефакт не соответствует закреплённой архитектуре Qwen3')
        torch.set_num_threads(4)
        torch.manual_seed(self.runtime['generation']['seed'])
        self.tokenizer = AutoTokenizer.from_pretrained(artifact, local_files_only=True)
        if tokenizer_hash(artifact) != self.runtime['tokenizer_sha256']:
            raise ValueError('Изменились файлы токенизатора')
        template = self.tokenizer.chat_template
        if sha256(canonical(template)) != self.runtime['template_sha256']:
            raise ValueError('Изменился шаблон чата')
        self.model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32,
                                                        local_files_only=True).to('cpu').eval()
        if manifest['kind'] == 'adapter':
            from peft import PeftModel
            self.model = PeftModel.from_pretrained(self.model, artifact, local_files_only=True).eval()

    def warmup(self):
        self.generate('Этот фильм просто замечательный, я в восторге!', self.runtime['generation']['max_new_tokens'])

    def generate(self, prompt, limit):
        import torch

        generation = self.runtime['generation']
        text = self.tokenizer.apply_chat_template(
            [{'role': 'system', 'content': generation['system']}, {'role': 'user', 'content': prompt}],
            tokenize=False, add_generation_prompt=True, enable_thinking=generation['enable_thinking'])
        batch = self.tokenizer(text, return_tensors='pt')
        if batch['input_ids'].shape[1] > 2048:
            raise ValueError('Вход превышает ограничение API: 2048 токенов с шаблоном')
        self.calls += 1
        with torch.inference_mode():
            output = self.model.generate(**batch, max_new_tokens=limit, do_sample=False,
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
        answer_ids = output[0, batch['input_ids'].shape[1]:]
        return {'answer': self.tokenizer.decode(answer_ids, skip_special_tokens=True),
                'output_tokens': len(answer_ids)}
