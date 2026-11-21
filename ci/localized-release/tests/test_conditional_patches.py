"""Real Git/index proofs for the narrowly conditional preferences maintenance patch."""
from contextlib import contextmanager
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lightweight_fixture import lightweight_fixture, prepare_fixture, workspace

w = workspace.watcher()
APP = 'src/lib/app/RvCommon/RvApplication.cpp'
HEADER = 'src/lib/app/RvCommon/RvCommon/RvApplication.h'
DOCUMENT = 'src/lib/app/RvCommon/RvDocument.cpp'
OLD = {APP: 'RvPreferences* prefDialog() { return cachedPreferences; }\n',
       HEADER: 'RvPreferences* cachedPreferences;\n',
       DOCUMENT: 'void exitDocument() { lazyPreferences()->close(); }\n'}
FIXED = {APP: 'RvPreferences* prefDialog() { return cachedPreferences.data(); }\n',
         HEADER: 'QPointer<RvPreferences> cachedPreferences;\n',
         DOCUMENT: 'void exitDocument() { closeExistingAuxiliaryWindows(); }\n'}


@contextmanager
def conditional_fixture(state='old'):
    with lightweight_fixture(optional='present') as f:
        upstream, trusted, git, write = f['upstream'], f['trusted'], f['git'], f['write']
        for path, text in OLD.items():
            write(upstream, path, text)
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'old preferences source')
        for path, text in FIXED.items():
            write(upstream, path, text)
        patch = subprocess.check_output(['git', '-C', str(upstream), 'diff', '--binary', '--full-index',
                                         '--', *sorted(OLD)], env=w.git_environment())
        git(upstream, 'checkout', '--', *sorted(OLD))
        if state == 'fixed':
            for path, text in FIXED.items():
                write(upstream, path, text)
        elif state == 'partial':
            write(upstream, HEADER, FIXED[HEADER])
        elif state == 'conflict':
            write(upstream, APP, 'RvPreferences* prefDialog() { return differentUpstreamOwnership(); }\n')
        elif state != 'old':
            raise AssertionError(state)
        if state != 'old':
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-m', 'reviewed fixture upstream variant ' + state)
        official = git(upstream, 'rev-parse', 'HEAD')
        git(upstream, 'tag', '-f', f['release'].tag)
        f['release'] = replace(f['release'], commit_sha=official, object_sha=official)
        write(trusted, w.PREFERENCES_PATCH, patch)
        config = f['config']
        config['patches'].append({'path': w.PREFERENCES_PATCH, 'sha256': hashlib.sha256(patch).hexdigest(),
                                  'optional_component': None, 'condition': w.PREFERENCES_CONDITION})
        write(trusted, w.LOCALIZATION_BASE_FILE, json.dumps(config, indent=2) + '\n')
        git(trusted, 'add', '.')
        git(trusted, 'commit', '-m', 'declare exact conditional maintenance patch')
        f['snapshot'] = git(trusted, 'rev-parse', 'HEAD')
        f['conditional_patch'] = patch
        yield f


class ConditionalPatchTests(unittest.TestCase):
    def assert_fixed_source(self, f, plan):
        source = f['root'] / 'source'
        for path, expected in FIXED.items():
            self.assertEqual(f['git'](source, 'show', plan['composed_tree'] + ':' + path), expected.strip())
        # A conditional decision must not suppress core or optional localization.
        self.assertEqual(f['git'](source, 'show', plan['composed_tree'] + ':caption.txt'), 'Localized caption')
        self.assertEqual(f['git'](source, 'show', plan['composed_tree']
                                 + ':src/plugins/rv-packages/annotate_beta/caption.py'), 'translated optional caption')

    def test_old_source_applies_fix_and_serializes_replayable_decision(self):
        with conditional_fixture() as f:
            plan, _, output = prepare_fixture(f)
            self.assert_fixed_source(f, plan)
            decisions = plan['conditional_patch_decisions']
            self.assertEqual(len(decisions), 1)
            self.assertEqual((decisions[0]['decision'], decisions[0]['reason']), ('applied', 'forward-apply-check'))
            self.assertEqual(decisions[0]['sha256'], hashlib.sha256(f['conditional_patch']).hexdigest())
            self.assertNotEqual(decisions[0]['input_tree'], decisions[0]['output_tree'])
            self.assertEqual(json.loads((output / 'build-plan.json').read_text())['conditional_patch_decisions'], decisions)
            self.assertEqual(workspace.verify_materialized(f['root'] / 'source', plan, b''), plan)
            for path, text in OLD.items():
                self.assertEqual(f['git'](f['upstream'], 'show', f['release'].commit_sha + ':' + path), text.strip())

    def test_complete_upstream_fix_is_skipped_only_after_exact_reverse_check(self):
        with conditional_fixture('fixed') as f:
            plan, _, _ = prepare_fixture(f)
            self.assert_fixed_source(f, plan)
            decision = plan['conditional_patch_decisions'][0]
            self.assertEqual((decision['decision'], decision['reason']), ('already-fixed', 'reverse-apply-check'))
            self.assertEqual(decision['input_tree'], decision['output_tree'])
            self.assertEqual(workspace.verify_materialized(f['root'] / 'source', plan, b''), plan)

    def test_partial_qpointer_or_unknown_source_requires_review_without_mutating_controller(self):
        for state in ('partial', 'conflict'):
            with self.subTest(state=state), conditional_fixture(state) as f:
                before = f['git'](f['trusted'], 'rev-parse', 'HEAD')
                with self.assertRaisesRegex(RuntimeError, 'partial or conflicts.*review'):
                    prepare_fixture(f)
                self.assertEqual(f['git'](f['trusted'], 'rev-parse', 'HEAD'), before)
                self.assertEqual(f['git'](f['trusted'], 'status', '--porcelain'), '')
                self.assertFalse((f['root'] / 'source-input/build-plan.json').exists())

    def test_conditional_decision_cannot_be_removed_relabelled_or_forged(self):
        with conditional_fixture() as f:
            plan, _, _ = prepare_fixture(f)
            variants = []
            missing = copy.deepcopy(plan); del missing['conditional_patch_decisions']; variants.append(missing)
            forged = copy.deepcopy(plan); forged['conditional_patch_decisions'][0]['sha256'] = '0' * 64; variants.append(forged)
            skipped = copy.deepcopy(plan)
            decision = skipped['conditional_patch_decisions'][0]
            decision.update(decision='already-fixed', reason='reverse-apply-check', output_tree=decision['input_tree'])
            variants.append(skipped)
            duplicate = copy.deepcopy(plan); duplicate['conditional_patch_decisions'] *= 2; variants.append(duplicate)
            for altered in variants:
                with self.subTest(altered=altered.get('conditional_patch_decisions')), self.assertRaises(ValueError):
                    workspace.verify_composition(f['root'] / 'source', altered, b'')

    def test_historical_plan4_without_a_conditional_field_still_verifies(self):
        with lightweight_fixture() as f:
            plan, _, _ = prepare_fixture(f)
            self.assertEqual(plan.pop('conditional_patch_decisions'), [])
            self.assertEqual(workspace.verify_composition(f['root'] / 'source', plan, b''), f['release'])

    def test_condition_scope_unknown_keys_order_and_overlay_overrides_are_rejected(self):
        with conditional_fixture() as f:
            good = f['config']
            variants = []
            wrong = copy.deepcopy(good); wrong['unknown'] = True; variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['patches'][0]['condition'] = w.PREFERENCES_CONDITION; variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['patches'][-1]['condition'] = 'guess-from-keywords'; variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['patches'][-1]['unknown'] = True; variants.append(wrong)
            wrong = copy.deepcopy(good); del wrong['patches'][-1]['condition']; variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['patches'][-1]['optional_component'] = good['optional_upstream_components'][0]; variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['patches'].reverse(); variants.append(wrong)
            wrong = copy.deepcopy(good); wrong['owned_files'][0]['destination'] = HEADER; variants.append(wrong)
            for config in variants:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    w.validate_bundle_configuration(config)

    def test_condition_cannot_be_used_to_skip_a_patch_with_a_different_file_scope(self):
        with conditional_fixture() as f:
            trusted, git, write = f['trusted'], f['git'], f['write']
            # Use the real core patch bytes; its path scope is not the three
            # preferences paths even when the conditional manifest record is valid.
            other_patch = (trusted / 'patches/core.patch').read_bytes()
            write(trusted, w.PREFERENCES_PATCH, other_patch)
            f['config']['patches'][-1]['sha256'] = hashlib.sha256(other_patch).hexdigest()
            write(trusted, w.LOCALIZATION_BASE_FILE, json.dumps(f['config']) + '\n')
            git(trusted, 'add', '.'); git(trusted, 'commit', '-m', 'invalid conditional source scope')
            f['snapshot'] = git(trusted, 'rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'exactly its three reviewed source paths'):
                prepare_fixture(f)


if __name__ == '__main__':
    unittest.main()
