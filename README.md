# Deadlock hero cull fix

For Deadlock build 6726. The mod **must** be used together with the cvars in step 3.

## Install

1. Download `pak01_dir.vpk` from the [latest release](../../releases/latest) and put it here:
   ```
   Deadlock/game/citadel/cullfix/pak01_dir.vpk
   ```

2. In `Deadlock/game/citadel/gameinfo.gi`, make the `SearchPaths` block look like this:
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
   The `Mod` and `Write` lines are required. Without them the game crashes on launch.

3. **Required:** in the `ConVars` block of the same `gameinfo.gi`, set:
   ```
   r_size_cull_threshold      "6.4"
   citadel_unit_status_width  "2000"
   ```

## Uninstall

Remove the `Game "citadel/cullfix"` line from `gameinfo.gi`.
