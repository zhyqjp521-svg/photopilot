"""Reproducible single-image render benchmark (no face-inference timing).

python scripts/bench_edit.py --root /path/to/checkout --iterations 3
The fixture and outputs live in an automatically cleaned temporary directory.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--iterations', type=int, default=3)
    args = ap.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from PIL import Image
    from photopilot import server
    times = []
    with tempfile.TemporaryDirectory(prefix='photopilot-edit-bench-') as tmp:
        source = Path(tmp) / 'photo.jpg'
        Image.open(root / 'demo/hopper.jpg').resize((3000, 2000)).save(source, quality=95)
        session = server._prepare({'folder': tmp, 'paths': [str(source)],
                                   'operation': 'polish', 'export_size': 2048,
                                   'polish': {'auto_tone': True}})
        # Fix face results across revisions to isolate the redundant rendering.
        with patch.object(server, 'analyze_faces', new=lambda _img: []):
            for _ in range(max(1, args.iterations)):
                start = time.perf_counter()
                result = server._process_one({'sid': session['sid'], 'path': str(source)})
                times.append(time.perf_counter() - start)
        size = Image.open(result['out']).size
    print(json.dumps({'root': str(root), 'seconds': times,
                      'median_seconds': statistics.median(times), 'output_size': size}))


if __name__ == '__main__':
    main()
