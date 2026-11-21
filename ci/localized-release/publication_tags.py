"""Own lightweight-release tags bind upstream identities as text, never Git ancestry."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess

UPSTREAM = 'AcademySoftwareFoundation/OpenRV'
TARGET = 'EternalSunLhx/OpenRV-Publish'
CONFIG_PATH = 'ci/upstream-release-watch/localization-base.json'
PUBLICATION_KIND = 'openrv-publication'
PUBLICATION_SCHEMA = 1
OFFICIAL_FIELDS = frozenset(('release_id', 'tag', 'object_sha', 'commit_sha', 'published_at'))
SHA = re.compile(r'[0-9a-f]{40}')
TAG = re.compile(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')


def checked_sha(value) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Expected a full immutable Git identity')
    return value


def official_record(release) -> dict:
    record = asdict(release) if is_dataclass(release) else release
    if not isinstance(record, dict) or set(record) != OFFICIAL_FIELDS:
        raise ValueError('Publication metadata requires the exact five official release fields')
    if (type(record['release_id']) is not int or record['release_id'] <= 0
            or not isinstance(record['tag'], str) or not TAG.fullmatch(record['tag'])):
        raise ValueError('Invalid official release identity')
    checked_sha(record['object_sha']); checked_sha(record['commit_sha'])
    published = record['published_at']
    if not isinstance(published, str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', published):
        raise ValueError('Official publication time must be a complete UTC timestamp')
    datetime.fromisoformat(published[:-1] + '+00:00')
    return dict(record)


def publication_tag_message(release) -> str:
    """The canonical annotation format is shared by creation and read-only monitoring."""
    return json.dumps({'kind': PUBLICATION_KIND, 'schema': PUBLICATION_SCHEMA,
                       'official': official_record(release)},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n'


def annotation_commit(annotation, release) -> str:
    record = official_record(release)
    if (not isinstance(annotation, dict) or annotation.get('tag') != record['tag']
            or annotation.get('message') != publication_tag_message(record)
            or not isinstance(annotation.get('object'), dict)
            or annotation['object'].get('type') != 'commit'):
        raise ValueError('Publication tag annotation differs from the immutable official release record')
    checked_sha(annotation.get('sha'))
    return checked_sha(annotation['object'].get('sha'))


def read_publication_tag(api, release):
    record = official_record(release)
    status, reference = api.request('GET', TARGET, 'git/ref/tags/' + record['tag'])
    if status == 404:
        return None
    if status != 200 or not isinstance(reference, dict):
        raise RuntimeError('Publication tag lookup failed (HTTP ' + str(status) + ')')
    obj = reference.get('object')
    if (reference.get('ref') != 'refs/tags/' + record['tag'] or not isinstance(obj, dict)
            or obj.get('type') != 'tag'):
        raise ValueError('Publication ref must be an annotated tag for the requested official version')
    identity = checked_sha(obj.get('sha'))
    status, annotation = api.request('GET', TARGET, 'git/tags/' + identity)
    if status != 200 or not isinstance(annotation, dict):
        raise RuntimeError('Publication annotation lookup failed (HTTP ' + str(status) + ')')
    if annotation.get('sha') != identity:
        raise ValueError('Publication annotation object identity differs')
    return {'reference': reference, 'annotation': annotation, 'commit': annotation_commit(annotation, record)}


def is_light_snapshot(repo: Path, candidate: str, snapshot: str) -> bool:
    """Read local objects only: accept schema-2 maintenance ancestry, never fetch it."""
    checked_sha(candidate); checked_sha(snapshot)
    ancestry = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', candidate, snapshot],
                              capture_output=True, timeout=30)
    if ancestry.returncode:
        return False
    configuration = subprocess.run(['git', '-C', str(repo), 'show', candidate + ':' + CONFIG_PATH],
                                   capture_output=True, timeout=30)
    if configuration.returncode:
        return False
    try:
        config = json.loads(configuration.stdout)
    except (ValueError, UnicodeError):
        return False
    return (isinstance(config, dict) and type(config.get('schema')) is int
            and config['schema'] == 2 and config.get('repository') == UPSTREAM)


def resolve_publication_commit(repo: Path, snapshot: str, api, release) -> str:
    snapshot = checked_sha(snapshot)
    if not is_light_snapshot(repo, snapshot, snapshot):
        raise ValueError('Current maintenance snapshot is not a schema-2 lightweight repository')
    publication = read_publication_tag(api, release)
    if publication is None:
        return snapshot
    anchor = publication['commit']
    if not is_light_snapshot(repo, anchor, snapshot):
        raise ValueError('Publication anchor is not a schema-2 ancestor of the current maintenance snapshot')
    return anchor


def verify_publication_tag(api, release, publication_commit: str) -> dict:
    expected = checked_sha(publication_commit)
    publication = read_publication_tag(api, release)
    if publication is None:
        raise ValueError('Expected immutable publication tag is missing')
    if publication['commit'] != expected:
        raise ValueError('Publication tag points to a different maintenance commit')
    return publication['reference']


def sync_publication_tag(repo: Path, plan: dict, api) -> bool:
    """Create only own annotation/ref objects; never push, replace or delete refs.

    The caller verifies composition and current official release metadata first.
    Tagger fields are intentionally omitted; GitHub supplies its authenticated identity.
    """
    release = official_record(plan['official'])
    snapshot = checked_sha(plan['localization_snapshot'])
    anchor = checked_sha(plan['publication_commit'])
    if not is_light_snapshot(repo, snapshot, snapshot) or not is_light_snapshot(repo, anchor, snapshot):
        raise ValueError('Publication plan must anchor in schema-2 maintenance ancestry')
    existing = read_publication_tag(api, release)
    if existing is not None:
        if existing['commit'] != anchor:
            raise ValueError('Existing publication tag points to a different maintenance commit')
        return False
    if anchor != snapshot:
        raise ValueError('Previously resolved publication anchor disappeared; no tag will be recreated')
    payload = {'tag': release['tag'], 'message': publication_tag_message(release),
               'object': anchor, 'type': 'commit'}
    status, annotation = api.request('POST', TARGET, 'git/tags', payload)
    if status != 201:
        raise RuntimeError('Publication annotation creation failed (HTTP ' + str(status) + ')')
    if annotation_commit(annotation, release) != anchor:
        raise ValueError('Created publication annotation has a different maintenance anchor')
    status, unused = api.request('POST', TARGET, 'git/refs',
                                {'ref': 'refs/tags/' + release['tag'], 'sha': annotation['sha']})
    if status not in (201, 422):
        raise RuntimeError('Publication ref creation failed (HTTP ' + str(status) + ')')
    # A concurrent creator may use a different tagger/time, hence a different tag SHA.
    # Only identical canonical provenance and the planned maintenance commit qualify.
    verify_publication_tag(api, release, anchor)
    return status == 201
