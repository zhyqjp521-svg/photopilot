"""Benchmark smooth_skin against the frozen legacy reference in this checkout.

Run with the project's Python environment: python scripts/bench_skin.py
Uses synthetic RGB pixels and a soft mask only; writes no image or result files.
Requires a source checkout including tests/test_guided_filter_performance.py.
Timings cover smooth_skin, not the full render or face-analysis pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be an integer') from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--widths', type=positive_int, nargs='+', default=[1600, 3000],
                        help='image widths; height is rounded to a 3:2 aspect ratio')
    parser.add_argument('--iterations', type=positive_int, default=7,
                        help='measured pairs per size, after one warmup pair (default: 7)')
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests'))
    import cv2
    import numpy as np
    from photopilot.polish import smooth_skin
    from test_guided_filter_performance import legacy_smooth_skin

    results = []
    for width in args.widths:
        height = max(1, round(width * 2 / 3))
        rng = np.random.default_rng(927)
        # Spatial texture, channel variation and grain exercise full RGB smoothing.
        x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
        y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
        texture = 10 * np.sin(85 * x) * np.cos(61 * y)
        image = np.empty((height, width, 3), dtype=np.uint8)
        for channel, base in enumerate((160, 120, 100)):
            grain = rng.normal(0, 7, (height, width)).astype(np.float32)
            image[..., channel] = np.clip(
                base + 35 * x + 20 * y + texture + grain, 0, 255
            ).astype(np.uint8)
        mask = np.exp(-(((x - 0.5) / 0.38) ** 2 +
                        ((y - 0.5) / 0.46) ** 2)).astype(np.float32)
        before = (hashlib.sha256(image).hexdigest(), hashlib.sha256(mask).hexdigest())
        functions = {'legacy': legacy_smooth_skin, 'current': smooth_skin}

        warmup = {name: function(image, 0.6, 0.4, mask)
                  for name, function in functions.items()}
        np.testing.assert_array_equal(warmup['legacy'], warmup['current'])
        del warmup
        samples = {'legacy': [], 'current': []}
        orders = []
        for iteration in range(args.iterations):
            order = ['legacy', 'current'] if iteration % 2 == 0 else ['current', 'legacy']
            outputs = {}
            for name in order:
                start = perf_counter()
                output = functions[name](image, 0.6, 0.4, mask)
                samples[name].append((perf_counter() - start) * 1000)
                outputs[name] = output
            np.testing.assert_array_equal(outputs['legacy'], outputs['current'])
            orders.append(order)
            del outputs, output
        after = (hashlib.sha256(image).hexdigest(), hashlib.sha256(mask).hexdigest())
        if before != after:
            raise AssertionError('smooth_skin changed the synthetic source or mask')
        medians = {name: statistics.median(times) for name, times in samples.items()}
        results.append({
            'width': width, 'height': height, 'iterations': args.iterations,
            'samples_ms': samples, 'median_ms': medians,
            'time_reduction_percent': 100 * (1 - medians['current'] / medians['legacy']),
            'pair_orders': orders, 'pixels_equal': True, 'inputs_unchanged': True,
        })
    print(json.dumps({
        'scope': 'smooth_skin only; excludes full render and face analysis',
        'warmup_pairs': 1, 'strength': 0.6, 'retain': 0.4,
        'opencv_version': cv2.__version__, 'opencv_threads': cv2.getNumThreads(),
        'results': results,
    }, indent=2))


if __name__ == '__main__':
    main()
