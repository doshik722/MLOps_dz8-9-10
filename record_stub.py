"""Сохранение фактических HTTP-ответов опыта STUB A → B → A."""
from pathlib import Path

from integration_tests.test_incidents import IncidentTests
from release import write_json


def main():
    client = IncidentTests('test_http_loaded_version_and_rollback')
    client.setUp()
    original = client.http
    events = []

    def record(path, payload=None):
        status, response = original(path, payload)
        if path == '/generate':
            events.append({'request': payload, 'status': status, 'response': response})
        return status, response

    client.http = record
    try:
        client.test_http_loaded_version_and_rollback()
        ids = [row['response']['model_id'] for row in events]
        assert ids == ['fixture-A', 'fixture-A', 'fixture-B', 'fixture-A'], ids
        write_json(Path('evidence/stub-rollback.json'), {'synthetic': True,
                   'observed_models': ids, 'events': events})
        print('PASS: реальные HTTP-ответы STUB:', ' → '.join(ids))
    finally:
        client.doCleanups()


if __name__ == '__main__':
    main()
