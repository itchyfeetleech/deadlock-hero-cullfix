"""Deadlock hero cull fix: keep heroes from being size-culled when r_size_cull_threshold is raised.

Every hero model's render bounds are the union of per-bone spheres (m_modelSkeleton.m_boneSphere,
one radius per bone). This raises the pelvis sphere so the culling test sees heroes as larger.
Nothing else in the model changes: meshes, hitboxes, collision, physics and animation blocks are
copied byte-for-byte, and the rebuilt DATA block is verified field-by-field against the original.

Size culling (scenesystem.dll) compares half the bounding box's diagonal / distance against the
threshold, so scaling that half-diagonal by THRESHOLD / DEFAULT culls each hero at Valve's default
distance. The radius is solved per model from its bind-pose bounds. LOD selection uses the object's
transform scale, not its bounds, so LODs are unaffected and m_lodGroupSwitchDistances stays as is.

Usage: build.py [--pak .../game/citadel/pak01_dir.vpk] [--threshold 6.4] [--default 0.8] [--radius R (fixed radius instead)] [--out dist/pak01_dir.vpk]
Install: game/citadel/cullfix/pak01_dir.vpk plus `Game citadel/cullfix` before `Game citadel` in gameinfo.gi.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
from s2res import Resource, kv3_read, kv3_write, vpk_build  # noqa: E402
from vpk import VPK  # noqa: E402

PAK = Path.home() / ".local/share/Steam/steamapps/common/Deadlock/game/citadel/pak01_dir.vpk"
TAG = "CCitadelHeroModelGameData_t"


def hero_models(vpk: VPK) -> list[str]:
    out = []
    for path in sorted(p for p in vpk.entries if p.startswith("models/heroes") and p.endswith(".vmdl_c")):
        try:
            _, root = kv3_read(Resource.parse(vpk.read(path)).get("DATA"))
        except ImportError:
            raise
        except Exception:
            continue
        if TAG in (root["m_modelInfo"].get("m_keyValueText") or ""):
            out.append(path)
    return out


def _qmul(a, b):
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return (w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2, w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2, w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2)


def _qrot(q, p):
    x, y, z, w = q
    t = [2 * (y * p[2] - z * p[1]), 2 * (z * p[0] - x * p[2]), 2 * (x * p[1] - y * p[0])]
    return [p[0] + w * t[0] + y * t[2] - z * t[1], p[1] + w * t[1] + z * t[0] - x * t[2],
            p[2] + w * t[2] + x * t[1] - y * t[0]]


def half_diagonal(skel, override: dict[int, float] | None = None) -> float:
    """Half the diagonal of the bind-pose bounding box of all bone spheres (radius > 0)."""
    parents, pos, rot = list(skel["m_nParent"]), skel["m_bonePosParent"], skel["m_boneRotParent"]
    world, quat = [], []
    for i, parent in enumerate(parents):
        p, q = [float(v) for v in pos[i]], tuple(float(v) for v in rot[i])
        if parent < 0:
            world.append(p)
            quat.append(q)
        else:
            r = _qrot(quat[parent], p)
            world.append([world[parent][k] + r[k] for k in range(3)])
            quat.append(_qmul(quat[parent], q))
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for i, sphere in enumerate(skel["m_boneSphere"]):
        radius = (override or {}).get(i, float(sphere))
        if radius <= 0:
            continue
        for k in range(3):
            lo[k] = min(lo[k], world[i][k] - radius)
            hi[k] = max(hi[k], world[i][k] + radius)
    return 0.5 * math.dist(lo, hi)


def matched_radius(skel, bone: int, factor: float) -> tuple[float, float]:
    """Pelvis radius whose bounds half-diagonal is factor x the original. Returns (radius, original)."""
    original = half_diagonal(skel)
    lo, hi = float(skel["m_boneSphere"][bone]), factor * original
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if half_diagonal(skel, {bone: mid}) < factor * original else (lo, mid)
    return hi, original


def patch(data: bytes, radius: float | None, factor: float) -> tuple[bytes, dict]:
    res = Resource.parse(data)
    index = next(i for i, (t, _) in enumerate(res.blocks) if t == "DATA")
    fmt, root = kv3_read(res.blocks[index][1])
    original = copy.deepcopy(root)
    skel = root["m_modelSkeleton"]
    bone = list(skel["m_boneName"]).index("pelvis")
    spheres = skel["m_boneSphere"]
    before = float(spheres[bone])
    old_half = half_diagonal(skel)
    if radius is None:
        radius, _ = matched_radius(skel, bone, factor)
    spheres[bone] = type(spheres[bone])(max(before, radius))
    res.blocks[index][1] = kv3_write(root, fmt)
    rebuilt = Resource.parse(res.build())

    # Verify: every block except DATA is byte-identical, and DATA differs only in that one radius.
    old = Resource.parse(data)
    assert [t for t, _ in rebuilt.blocks] == [t for t, _ in old.blocks]
    for (t, a), (_, b) in zip(old.blocks, rebuilt.blocks):
        if t != "DATA":
            assert a == b, f"block {t} changed"
    _, check = kv3_read(rebuilt.get("DATA"))
    assert float(check["m_modelSkeleton"]["m_boneSphere"][bone]) == max(before, radius)
    check["m_modelSkeleton"]["m_boneSphere"][bone] = original["m_modelSkeleton"]["m_boneSphere"][bone]
    assert check == original, "DATA changed beyond the pelvis sphere"
    new_half = half_diagonal(skel)
    return res.build(), {"pelvis_radius_before": round(before, 3), "after": round(float(spheres[bone]), 3),
                         "bounds_half_diagonal": round(old_half, 2), "scale": round(new_half / old_half, 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=6.4, help="your r_size_cull_threshold")
    ap.add_argument("--default", type=float, default=0.8, help="Valve's default r_size_cull_threshold")
    ap.add_argument("--radius", type=float, help="fixed pelvis radius for every hero instead of matching")
    ap.add_argument("--pak", type=Path, default=PAK, help="Deadlock's game/citadel/pak01_dir.vpk")
    ap.add_argument("--out", type=Path, default=HERE / "dist/pak01_dir.vpk")
    args = ap.parse_args()
    factor = args.threshold / args.default
    vpk = VPK(args.pak)
    files, report = {}, []
    for path in hero_models(vpk):
        files[path], info = patch(vpk.read(path), args.radius, factor)
        report.append({"model": path, **info})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(vpk_build(files))
    mode = {"radius": args.radius} if args.radius else {"threshold": args.threshold, "default": args.default,
                                                        "factor": factor}
    (args.out.parent / "manifest.json").write_text(json.dumps({**mode, "models": report}, indent=1))
    radii = sorted(r["after"] for r in report)
    print(f"{len(files)} hero models patched; pelvis radius {radii[0]:.0f}-{radii[-1]:.0f} "
          f"(median {radii[len(radii) // 2]:.0f}); wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
