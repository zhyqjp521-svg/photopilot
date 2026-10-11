"""Frozen export settings are independent of live sessions and source references."""
import copy
import importlib
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path

import numpy as np

from photopilot.color import ColorReference, match_color
from photopilot.io import collision_safe_output_names
from photopilot.polish import PolishParams, apply_wb_gains, polish_image


class ExportRecipeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('photopilot.export_recipe'),
                             'Frozen prepared-session codec is missing')
        self.codec = importlib.import_module('photopilot.export_recipe')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.paths = [str(self.root / 'a' / 'same.png'), str(self.root / 'b' / 'same.png')]
        self.session = dict(folder=self.root, operation='both', do_color=True,
                            do_polish=True, algo='oklab', strength=.8,
                            preserve_luma=False, skin_protect=None,
                            params=PolishParams(global_skin=False, exposure=.3),
                            paths=self.paths, output_names=collision_safe_output_names(self.paths),
                            export_size=2048, export_quality=95, scene_ref=False,
                            wb_mode='off', wb_strength=.95, reference=None,
                            ref_desc='仅白平衡/美化（不追色）')

    def roundtrip(self, session=None):
        recipe = self.codec.freeze_session(session or self.session)
        return recipe, self.codec.thaw_session(json.loads(json.dumps(recipe, allow_nan=False)))

    def test_all_polish_fields_and_no_reference_roundtrip(self):
        recipe, thawed = self.roundtrip()
        self.assertEqual(recipe['version'], 1)
        self.assertEqual(asdict(thawed['params']), asdict(self.session['params']))
        self.assertEqual(thawed['folder'], self.root)
        self.assertIsNone(thawed['reference'])
        self.assertEqual(thawed['output_names'], self.session['output_names'])
        self.assertIsNot(thawed['params'], self.session['params'])

    def test_preset_and_stats_references_roundtrip(self):
        refs = [ColorReference('preset', preset='wedding_airy'),
                ColorReference('stats', stats=(np.array([.5, -.02, .03], np.float32),
                                               np.array([.1, .2, .3], np.float32)))]
        for reference in refs:
            self.session['reference'] = reference
            reference._cache['not serializable'] = object()
            _, thawed = self.roundtrip()
            self.assertEqual(thawed['reference'].kind, reference.kind)
            self.assertEqual(thawed['reference']._cache, {})
            if reference.stats:
                for before, after in zip(reference.stats, thawed['reference'].stats):
                    np.testing.assert_array_equal(before, after)
                    self.assertEqual(after.dtype, np.float32)

    def test_image_reference_pixels_and_render_are_identical(self):
        img = np.random.default_rng(5).integers(0, 256, (35, 50, 3), dtype=np.uint8)
        ref = img[::-1, ::-1].copy()
        self.session['reference'] = ColorReference('image', image=ref)
        for algo in ['oklab', 'reinhard', 'mkl', 'histogram', 'luma_hist']:
            self.session['algo'] = algo
            _, thawed = self.roundtrip()
            np.testing.assert_array_equal(thawed['reference'].image, ref)
            kwargs = dict(algo=algo, strength=.8, preserve_luma=False, skin_protect=None, faces=[])
            np.testing.assert_array_equal(match_color(img, self.session['reference'], **kwargs),
                                          match_color(img, thawed['reference'], **kwargs))
            self.assertIsNot(thawed['reference'].image, ref)

    def test_scene_stats_and_batch_wb_render_roundtrip(self):
        ref = ColorReference('stats', stats=(np.array([.6, .01, -.02], np.float32),
                                            np.array([.2, .03, .04], np.float32)))
        self.session.update(scene_ref=True, reference=ref,
                            scene_refs={p: ref for p in self.paths}, wb_mode='batch',
                            wb_target=np.array([120, 100, 90], np.float32),
                            wb_gains={p: np.array([1.2, .9, 1.1], np.float32) for p in self.paths})
        _, thawed = self.roundtrip()
        source = np.random.default_rng(8).integers(0, 256, (60, 80, 3), dtype=np.uint8)
        def render(session):
            img = apply_wb_gains(source, session['wb_gains'][self.paths[0]], session['wb_strength'])
            img = match_color(img, session['scene_refs'][self.paths[0]], strength=session['strength'])
            return polish_image(img, session['params'], faces=[])
        np.testing.assert_array_equal(render(self.session), render(thawed))
        self.assertIsNot(thawed['scene_refs'][self.paths[0]], thawed['scene_refs'][self.paths[1]])

    def test_polish_only_and_single_wb_valid(self):
        self.session.update(operation='polish', do_color=False, wb_mode='single', algo='none')
        _, thawed = self.roundtrip()
        self.assertFalse(thawed['do_color'])
        self.assertEqual(thawed['wb_mode'], 'single')

    def test_unknown_schema_and_malformed_values_rejected(self):
        recipe, _ = self.roundtrip()
        changes = [('version', True), ('version', 2), ('extra', 1), ('strength', True),
                   ('strength', float('nan')), ('strength', 2), ('export_size', True),
                   ('export_size', 1234), ('export_quality', 74), ('export_quality', 95.5),
                   ('preserve_luma', 1), ('skin_protect', 'auto'), ('algo', 'evil'),
                   ('operation', 'evil'), ('do_color', False), ('wb_mode', 'evil'),
                   ('wb_strength', -1), ('folder', 'relative'), ('folder', 'https://evil'),
                   ('paths', ['relative.png']), ('paths', ['file:///tmp/a.png']),
                   ('ref_desc', {'oops': 1})]
        for key, value in changes:
            bad = copy.deepcopy(recipe); bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.codec.thaw_session(bad)

    def test_unknown_session_keys_and_wrong_operation_ref_rejected(self):
        self.session['output_dir'] = '/tmp/evil'
        with self.assertRaises(ValueError): self.codec.freeze_session(self.session)
        del self.session['output_dir']
        self.session.update(operation='color', do_polish=False)
        with self.assertRaises(ValueError): self.codec.freeze_session(self.session)
        self.session['reference'] = ColorReference('preset', preset='natural')
        with self.assertRaises(ValueError): self.codec.freeze_session(self.session)

    def test_polish_schema_ranges_and_boolean_types_rejected(self):
        recipe, _ = self.roundtrip()
        for key, value in [('wb', True), ('skin', -1), ('exposure', 3.01),
                           ('contrast', -1.01), ('eyes', 1), ('global_skin', 'yes'),
                           ('local_region', 'unknown'), ('extra', 1)]:
            bad = copy.deepcopy(recipe); bad['params'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_output_names_cannot_be_injected_or_collide(self):
        recipe, _ = self.roundtrip()
        for name in ['../evil.png', '/tmp/evil.png', 'evil.png', 'folder\\evil.png']:
            bad = copy.deepcopy(recipe); bad['output_names'][self.paths[0]] = name
            with self.subTest(name=name), self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_reference_variants_reject_extra_fields_and_invalid_values(self):
        recipe, _ = self.roundtrip()
        references = [{'kind': 'preset', 'preset': 'unknown'},
                      {'kind': 'preset', 'preset': 'natural', '_cache': {}},
                      {'kind': 'stats', 'mean': [0, 1], 'std': [.1, .2, .3]},
                      {'kind': 'stats', 'mean': [0, float('inf'), 1], 'std': [.1, .2, .3]},
                      {'kind': 'stats', 'mean': [0, 0, 0], 'std': [-1, .2, .3]},
                      {'kind': 'image', 'shape': [1, 1, 3], 'data': '!!!!'},
                      {'kind': 'image', 'shape': [1, 1, 3], 'data': 'AA=='},
                      {'kind': 'image', 'shape': [1025, 1, 3], 'data': ''},
                      {'kind': 'image', 'shape': [True, 1, 3], 'data': ''},
                      {'kind': 'image', 'shape': [1, 1, 4], 'data': ''}]
        for reference in references:
            bad = copy.deepcopy(recipe); bad['reference'] = reference
            with self.subTest(reference=reference), self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_scene_refs_must_be_stats_and_known_paths(self):
        recipe, _ = self.roundtrip()
        for mapping in [{'/unknown.png': None}, {self.paths[0]: {'kind': 'preset', 'preset': 'natural'}}]:
            bad = copy.deepcopy(recipe); bad['scene_refs'] = mapping
            with self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_batch_wb_requires_full_finite_vectors(self):
        recipe, _ = self.roundtrip()
        for gains, target in [({}, [1, 2, 3]), ({p: [1, 2] for p in self.paths}, [1, 2, 3]),
                              ({p: [True, 1, 1] for p in self.paths}, [1, 2, 3]),
                              ({p: [1, -1, 1] for p in self.paths}, [1, 2, 3]),
                              ({p: [1, 1, 1] for p in self.paths}, [0, 1, 2])]:
            bad = copy.deepcopy(recipe); bad.update(wb_mode='batch', wb_gains=gains, wb_target=target)
            with self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_bounded_paths_and_recipe_size(self):
        recipe, _ = self.roundtrip()
        bad = copy.deepcopy(recipe); bad['paths'] = self.paths * 10001
        with self.assertRaises(ValueError): self.codec.thaw_session(bad)
        bad = copy.deepcopy(recipe); bad['ref_desc'] = 'x' * (16 * 1024 * 1024)
        with self.assertRaises(ValueError): self.codec.thaw_session(bad)

    def test_total_recipe_size_checked_before_reference_decode(self):
        self.session['reference'] = ColorReference('image', image=np.zeros((1024, 1024, 3), np.uint8))
        recipe, _ = self.roundtrip()
        recipe['scene_ref'] = True
        recipe['scene_refs'] = {str(self.root / f'{i}.png'): recipe['reference'] for i in range(4)}
        recipe['paths'] = list(recipe['scene_refs'])
        recipe['output_names'] = collision_safe_output_names(recipe['paths'])
        with patch.object(self.codec.base64, 'b64decode', side_effect=AssertionError('decoded before size check')):
            with self.assertRaises(ValueError): self.codec.thaw_session(recipe)

    def test_unhashable_and_missing_session_values_raise_valueerror(self):
        for value in [[[]], [None], [123], []]:
            session = dict(self.session); session['paths'] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.codec.freeze_session(session)
        session = dict(self.session); del session['strength']
        with self.assertRaises(ValueError): self.codec.freeze_session(session)

    def test_freeze_scene_images_rejected_before_encoding(self):
        self.session.update(scene_ref=True, scene_refs={self.paths[0]:
                            ColorReference('image', image=np.zeros((10, 10, 3), np.uint8))})
        with patch.object(self.codec.base64, 'b64encode', side_effect=AssertionError('unexpected image encoding')):
            with self.assertRaises(ValueError): self.codec.freeze_session(self.session)

    def test_real_server_prepared_sessions_roundtrip_without_reference_file(self):
        from photopilot import server
        from PIL import Image
        sources = []
        for i in range(6):
            path = self.root / f'photo-{i}.png'
            Image.fromarray(np.random.default_rng(i).integers(0, 256, (16, 24, 3), dtype=np.uint8)).save(path)
            sources.append(str(path))
        target = self.root / 'reference.png'
        Image.fromarray(np.full((10, 20, 3), 128, np.uint8)).save(target)
        for extra in [dict(operation='polish'), dict(preset='wedding_airy'),
                      dict(operation='color', ref=str(target)), dict(scene_ref=True, wb_mode='batch')]:
            response = server._prepare(dict(folder=str(self.root), paths=sources, **extra))
            session = server._SESSIONS.pop(response['sid'])
            frozen = self.codec.freeze_session(session)
            if extra.get('operation') == 'color':
                target.unlink()
            restored = self.codec.thaw_session(json.loads(json.dumps(frozen)))
            self.assertEqual(asdict(restored['params']), asdict(session['params']))
            self.assertEqual(restored['paths'], session['paths'])
            if session.get('reference') and session['reference'].kind == 'image':
                np.testing.assert_array_equal(restored['reference'].image, session['reference'].image)

    def test_real_server_symlink_session_uses_canonical_output_name(self):
        from photopilot import server
        from PIL import Image
        source = self.root / 'real.png'
        Image.fromarray(np.full((10, 20, 3), 128, np.uint8)).save(source)
        alias = self.root / 'alias.png'
        alias.symlink_to(source)
        response = server._prepare(dict(folder=str(self.root), paths=[str(alias)],
                                        operation='polish'))
        session = server._SESSIONS.pop(response['sid'])
        self.assertEqual(session['output_names'], {str(alias): 'alias_pp.png'})
        recipe, restored = self.roundtrip(session)
        self.assertEqual(recipe['paths'], [str(source)])
        self.assertEqual(restored['output_names'], {str(source): 'real_pp.png'})
        self.assertEqual(session['output_names'], {str(alias): 'alias_pp.png'})

    def test_symlink_aliases_deduplicate_and_keep_frozen_wb_scene_maps(self):
        source = self.root / 'real.png'
        source.touch()
        aliases = [self.root / 'first.png', self.root / 'second.png']
        for alias in aliases:
            alias.symlink_to(source)
        paths = [str(alias) for alias in aliases]
        ref = ColorReference('stats', stats=(np.array([.6, .01, -.02], np.float32),
                                            np.array([.2, .03, .04], np.float32)))
        gains = np.array([1.2, .9, 1.1], np.float32)
        self.session.update(paths=paths, output_names=collision_safe_output_names(paths),
                            scene_ref=True, reference=ref, scene_refs={p: ref for p in paths},
                            wb_mode='batch', wb_target=np.array([120, 100, 90], np.float32),
                            wb_gains={p: gains for p in paths})
        recipe, restored = self.roundtrip()
        self.assertEqual(recipe['paths'], [str(source)])
        self.assertEqual(restored['output_names'], {str(source): 'real_pp.png'})
        self.assertEqual(set(restored['scene_refs']), {str(source)})
        self.assertEqual(set(restored['wb_gains']), {str(source)})
        np.testing.assert_array_equal(restored['wb_gains'][str(source)], gains)
        for before, after in zip(ref.stats, restored['scene_refs'][str(source)].stats):
            np.testing.assert_array_equal(before, after)

    def test_freeze_rejects_injected_prepared_output_names(self):
        for name in ['../evil.png', '/tmp/evil.png', 'evil.png', 'folder\\evil.png']:
            session = copy.deepcopy(self.session)
            session['output_names'][self.paths[0]] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.codec.freeze_session(session)


if __name__ == '__main__':
    unittest.main()
