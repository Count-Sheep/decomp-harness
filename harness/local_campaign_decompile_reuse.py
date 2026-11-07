"""Seed identical-draft caches; the ordinary validator must check every input key."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from local_campaign_decompile import write_json


def seed(baseline: Path, batch: Path, validation_root: Path) -> int:
    prior = json.loads((baseline / 'results.json').read_text(encoding='utf-8'))
    replay = json.loads((batch / 'results.json').read_text(encoding='utf-8'))
    summary = prior['summary']
    if not (summary.get('finished') and summary.get('environment_unchanged') is True
            and summary.get('validator_version', 0) >= 2 and summary.get('environment_sha256')):
        raise ValueError('A completed, stable, versioned baseline validation is required')
    if not replay['summary'].get('finished'):
        raise ValueError('Replay generation must be complete')
    if Path(replay['summary'].get('baseline', '')).resolve() != Path(summary['batch']).resolve():
        raise ValueError('Validation baseline must belong to the replayed generation batch')
    before = {row['id']: row for row in prior['results']}
    if before.keys() != {row['id'] for row in replay['results']}:
        raise ValueError('Replay and baseline must contain the same complete target set')
    count = 0
    for row in replay['results']:
        old = before[row['id']]
        destination = validation_root / batch.name / row['id'] / 'result.json'
        if destination.exists() or not old.get('input_sha256') or not row.get('artifacts'):
            continue
        draft = Path(row['artifacts']) / 'draft.c'
        if not draft.is_file() or hashlib.sha256(draft.read_bytes()).hexdigest() != old.get('draft_sha256'):
            continue
        # A cache seed is not a result: validate_one recomputes its full key before reuse.
        write_json(destination, {**old, 'reused_from_validation': str(baseline.resolve())})
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('batch', type=Path)
    parser.add_argument('--validation-root', type=Path,
                        help='default: <project>/build/decomp-support/<VERSION>/validation')
    args = parser.parse_args()
    if args.validation_root is None:
        from local_campaign_m2c_report import support_root
        args.validation_root = support_root() / 'validation'
    print(json.dumps({'seeded': seed(args.baseline, args.batch, args.validation_root)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
