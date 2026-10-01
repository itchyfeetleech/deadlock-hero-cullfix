"""Deadlock hero cull fix: keep heroes from being size-culled when r_size_cull_threshold is raised.

Every hero model's render bounds are the union of per-bone spheres (m_modelSkeleton.m_boneSphere,
one radius per bone). This raises the pelvis sphere so the culling test sees heroes as larger.
Nothing else in the model changes: meshes, hitboxes, collision, physics and animation blocks are
copied byte-for-byte, and the rebuilt DATA block is verified field-by-field against the original.

Size culling (scenesystem.dll) compares half the bounding box's diagonal / distance against the
threshold, so scaling that half-diagonal by THRESHOLD / DEFAULT culls each hero at Valve's default
distance. The radius is solved per model from its bind-pose bounds. LOD selection uses the object's
transform scale, not its bounds, so LODs are unaffected and m_lodGroupSwitchDistances stays as is.

Soul orbs are particle systems, so their bounds padding (m_BoundingBoxMin/Max) is scaled the same way,
and the sphere models their renderers draw (separate size-culled objects) get factor x bounds.

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
from s2res import KVArr, Resource, kv3_read, kv3_write, vpk_build  # noqa: E402
from vpk import VPK  # noqa: E402

PAK = Path.home() / ".local/share/Steam/steamapps/common/Deadlock/game/citadel/pak01_dir.vpk"
TAG = "CCitadelHeroModelGameData_t"


# Soul orbs (item_xp in scripts/misc.vdata) are model-less particle systems; these roots and their children.
ORB_PARTICLES = ["particles/generic/spirit_orb_ambient.vpcf_c", "particles/generic/spirit_orb_ambient_enemy.vpcf_c",
                 "particles/generic/spirit_orb_processing.vpcf_c"]
# Models the orb's particle renderers draw (C_OP_RenderModels); each is its own size-culled scene object.
ORB_MODELS = ["models/particle/aligned_sphere_fx_model.vmdl_c", "models/particle/sphere.vmdl_c"]
PARTICLE_DEFAULT_RADIUS = 5.0  # CParticleSystemDefinition m_flConstantRadius default
PARTICLE_DEFAULT_BOX = 10.0  # m_BoundingBoxMin/Max default (-10..10)
PARTICLE_MAX_HALF = 590.0  # keep boxes under r_particle_max_size_cull (1200), above which culling is skipped


def orb_particles(vpk: VPK) -> list[str]:
    out, todo = [], list(ORB_PARTICLES)
    while todo:
        path = todo.pop(0)
        if path in out or path not in vpk.entries:
            continue
        out.append(path)
        _, root = kv3_read(Resource.parse(vpk.read(path)).get("DATA"))
        todo += [str(c["m_ChildRef"]) + "_c" for c in root.get("m_Children", []) if c.get("m_ChildRef")]
    return out


def patch_particle(data: bytes, factor: float) -> tuple[bytes, dict]:
    """Scale a particle system's bounds padding so its half-diagonal grows about factor x.

    The system's bounds are its particles' extent plus m_BoundingBoxMin/Max. Treating the extent as
    +-m_flConstantRadius around the control point, padding p -> factor*p + (factor-1)*radius scales
    the whole box by factor.
    """
    res = Resource.parse(data)
    index = next(i for i, (t, _) in enumerate(res.blocks) if t == "DATA")
    fmt, root = kv3_read(res.blocks[index][1])
    original = copy.deepcopy(root)
    radius = float(root.get("m_flConstantRadius", PARTICLE_DEFAULT_RADIUS))
    grow = (factor - 1) * radius
    lo = [float(x) for x in root.get("m_BoundingBoxMin", [-PARTICLE_DEFAULT_BOX] * 3)]
    hi = [float(x) for x in root.get("m_BoundingBoxMax", [PARTICLE_DEFAULT_BOX] * 3)]
    root["m_BoundingBoxMin"] = KVArr(max(factor * v - grow, -PARTICLE_MAX_HALF) for v in lo)
    root["m_BoundingBoxMax"] = KVArr(min(factor * v + grow, PARTICLE_MAX_HALF) for v in hi)
    res.blocks[index][1] = kv3_write(root, fmt)

    old, rebuilt = Resource.parse(data), Resource.parse(res.build())
    for (t, a), (_, b) in zip(old.blocks, rebuilt.blocks):
        if t != "DATA":
            assert a == b, f"block {t} changed"
    _, check = kv3_read(rebuilt.get("DATA"))
    assert [float(v) for v in check["m_BoundingBoxMin"]] == list(root["m_BoundingBoxMin"])
    assert [float(v) for v in check["m_BoundingBoxMax"]] == list(root["m_BoundingBoxMax"])
    for key in ("m_BoundingBoxMin", "m_BoundingBoxMax"):
        check.pop(key)
        original.pop(key, None)
    assert check == original, "DATA changed beyond the bounding box"
    return res.build(), {"bbox_before": [lo, hi], "bbox_after": [list(root["m_BoundingBoxMin"]),
                                                                    list(root["m_BoundingBoxMax"])]}


def patch_static_model(data: bytes, factor: float) -> tuple[bytes, dict]:
    """Scale a static model's draw bounds (mesh scene-object bounds and bone spheres) by factor."""
    res = Resource.parse(data)
    old = Resource.parse(data)
    report = {}
    for i, (t, block) in enumerate(res.blocks):
        if t not in ("MDAT", "DATA"):
            continue
        fmt, root = kv3_read(block)
        if t == "MDAT":
            for n, obj in enumerate(root.get("m_sceneObjects", [])):
                lo, hi = [float(v) for v in obj["m_vMinBounds"]], [float(v) for v in obj["m_vMaxBounds"]]
                mid = [(a + b) / 2 for a, b in zip(lo, hi)]
                obj["m_vMinBounds"] = type(obj["m_vMinBounds"])(c + factor * (a - c) for a, c in zip(lo, mid))
                obj["m_vMaxBounds"] = type(obj["m_vMaxBounds"])(c + factor * (b - c) for b, c in zip(hi, mid))
                report[f"scene_object_{n}"] = [lo, hi]
            for bone in root.get("m_skeleton", {}).get("m_bones", []):
                bone["m_flSphereRadius"] = float(bone["m_flSphereRadius"]) * factor
        else:
            spheres = root["m_modelSkeleton"]["m_boneSphere"]
            for n in range(len(spheres)):
                spheres[n] = type(spheres[n])(float(spheres[n]) * factor)
        res.blocks[i][1] = kv3_write(root, fmt)
    rebuilt = Resource.parse(res.build())
    for (t, a), (_, b) in zip(old.blocks, rebuilt.blocks):
        if t not in ("MDAT", "DATA"):
            assert a == b, f"block {t} changed"
    return res.build(), {"bounds_before": report, "factor": factor}


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
    orbs = []
    for path in orb_particles(vpk):
        files[path], info = patch_particle(vpk.read(path), factor)
        orbs.append({"particle": path, **info})
    for path in ORB_MODELS:
        files[path], info = patch_static_model(vpk.read(path), factor)
        orbs.append({"model": path, **info})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(vpk_build(files))
    mode = {"radius": args.radius} if args.radius else {"threshold": args.threshold, "default": args.default,
                                                        "factor": factor}
    (args.out.parent / "manifest.json").write_text(json.dumps({**mode, "models": report, "soul_orbs": orbs}, indent=1))
    radii = sorted(r["after"] for r in report)
    print(f"{len(orbs)} soul-orb files patched; {len(report)} hero models patched; pelvis radius {radii[0]:.0f}-{radii[-1]:.0f} "
          f"(median {radii[len(radii) // 2]:.0f}); wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
