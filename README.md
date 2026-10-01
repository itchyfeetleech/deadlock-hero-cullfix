# Deadlock hero cull fix

Raising `r_size_cull_threshold` gives a big FPS boost, but it also culls distant heroes and their health bars. This mod keeps them on screen.

> [!IMPORTANT]
> **The VPK and the two cvars only work together.** You must set both of these in `gameinfo.gi` alongside the mod:
> ```
> r_size_cull_threshold      "6.4"
> citadel_unit_status_width  "2000"
> ```
> - **VPK without `r_size_cull_threshold "6.4"`:** it does nothing useful. At the default 0.8, heroes stay drawn 8× further out than normal, which costs FPS and gains you nothing.
> - **`6.4` without the VPK:** distant heroes vanish.
> - **No `citadel_unit_status_width "2000"`:** health bars still vanish at range.
>
> Install steps 3 **and** 4 below are both required.

> **Built for `r_size_cull_threshold "6.4"`** (Valve's default is `0.8`) on Deadlock build **6726** (Sep 30 2026).
> At 6.4, each hero now culls at the same distance Valve's default 0.8 culls it.
> If you use a different threshold, [build your own](#building) with `--threshold <your value>`.

## Install

1. Download `pak01_dir.vpk` from the [latest release](../../releases/latest).
2. Put it in a new `cullfix` folder:
   ```
   Deadlock/game/citadel/cullfix/pak01_dir.vpk
   ```
3. Edit `Deadlock/game/citadel/gameinfo.gi`. Find the `SearchPaths` block and replace the
   `Game citadel` / `Game core` lines with:
   ```
   SearchPaths
   {
       Game_UILanguage  "citadel_*LANGUAGE*"
       Game_LowViolence "citadel_lv"

       Game "citadel/cullfix"
       Mod "citadel"
       Write "citadel"
       Game "citadel"
       Write "core"
       Mod "core"
       Game "core"
   }
   ```
   If you already load other mods (e.g. `Game "citadel/addons"`), keep that line above `citadel/cullfix`.

   **Keep the `Mod` / `Write` lines.** Without them, the first `Game` path becomes the mod/write
   folder and the game crashes on launch ("Unable to read default keybinding configuration").
   Steam then marks the install as corrupt, and verifying the files resets `gameinfo.gi`.
4. **Required. The mod is matched to these values.** In the same `gameinfo.gi`, find the `ConVars` block and set both:
   ```
   r_size_cull_threshold      "6.4"    // [def: "0.8"] what this VPK is matched to
   citadel_unit_status_width  "2000"   // [def: "200"] keeps health bars from being culled (see below)
   ```

**Disable:** remove the `Game "citadel/cullfix"` line. The `Mod`/`Write` lines can stay; they do no harm.

**After a Deadlock update**, Steam may restore the stock `gameinfo.gi`. If so, redo steps 3–4. If the update changed the hero models, rebuild the VPK.

## How it works

### Heroes (the VPK)
A hero model's render bounds are the union of one sphere per bone (`m_modelSkeleton.m_boneSphere`).
Size culling (`scenesystem.dll`) measures an object as half its bounding-box diagonal ÷ distance and
culls it when that falls below the threshold. The mod enlarges only the **pelvis** sphere, so the
culling test sees each hero as bigger:

- The pelvis radius is solved per hero so the half-diagonal is `threshold ÷ default` times the
  original (6.4 ÷ 0.8 = **8×**, measured on the bind-pose bounds). Radii come out at 292–1728,
  median 426. Per-model values are in [`dist/manifest.json`](dist/manifest.json).
- All 62 hero models in `pak01_dir.vpk` are patched. They are identified by `CCitadelHeroModelGameData_t` in their key values.
- **Only that one float changes.** Meshes, hitboxes, collision, physics and animation are copied
  byte for byte, and the build checks that the rebuilt DATA block differs from the original only in that one value.
- **LODs are unaffected.** The engine picks the detail level from transform scale and distance, not from
  bounds.

### Health bars (gameinfo only)
Health bars are Panorama world panels (`unit_status_v2`), not models, so the VPK can't touch
them, and size culling culls them too. `citadel_unit_status_width "2000"` widens the panel's empty
canvas. The bar looks the same, but the culling test sees a bigger object.

- 2000 grows the panel's bounding radius about 6.9×, close to the ~7.1× needed for 6.4 to cull bars no sooner than they used to.
  800 was enough for hero bars but not trooper bars, which show further out.
- Don't raise `citadel_unit_status_height`. Taller panels float the bar higher above the unit.
- Keep width × `citadel_unit_status_window_scale` (2) ≤ 4096.
- Don't raise `citadel_unit_status_fadeout_dist`. That would show enemy health beyond the game's normal range.

### Far plane
If you also set `r_farz` / `r_mapextents` (e.g. `7000`), nothing beyond that distance draws, whatever
the bounds say. If heroes still vanish at a fixed distance, that is the cause.

## Building

Needs Python 3.10+ and the game files. The build reads the hero models straight from your
`pak01_dir.vpk`.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py --threshold 6.4            # match your r_size_cull_threshold
# options: --pak <path to game/citadel/pak01_dir.vpk>  (defaults to the Linux Steam path)
#          --default 0.8   Valve's default threshold to match against
#          --radius R      give every hero the same fixed pelvis radius instead
```

The output goes to `dist/pak01_dir.vpk` (+ `dist/manifest.json`). Copy it into `game/citadel/cullfix/`.

`tools/` holds a small, pure-Python Source 2 resource/KV3 reader-writer and a VPK reader.
