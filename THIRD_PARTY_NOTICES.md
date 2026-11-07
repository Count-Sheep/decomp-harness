# Third-party notices

Everything in this repository is MIT-licensed (see `LICENSE`) except the
components below, which keep their original licenses.

## `harness/m2c_patches/` — GPL-3.0

Patches against [m2c](https://github.com/matt-kempster/m2c), which is licensed
under the GNU General Public License, version 3. The patches and their
regression fixtures are distributed under the same license. See
`harness/m2c_patches/COPYING` and `harness/m2c_patches/NOTICE.txt` (base
revision and change list).

The harness applies these patches to a separate m2c checkout at runtime; m2c
itself is not included.

## `harness/permuter/` — MIT (upstream)

The owner extraction, owner-source guard, work-unit generator and farm
supervisor are adapted from MIT-licensed tooling published with an upstream
GameCube decompilation project. The upstream copyright notice is kept verbatim
in `harness/permuter/LICENSE`, as the MIT license requires. The code has been
modified here to remove project-specific cases and host paths.

## `harness/permuter/decomp_permuter_patches/` — MIT

Patches against
[decomp-permuter](https://github.com/simonlindholm/decomp-permuter)
(MIT, Copyright (c) 2019 Simon Lindholm and contributors), distributed under
the same license. See the `NOTICE.txt` in that directory. decomp-permuter
itself is not included.

## External tools (not included)

The harness drives tools that each project or user installs separately:
m2c, [decomp-permuter](https://github.com/simonlindholm/decomp-permuter),
[objdiff](https://github.com/encounter/objdiff),
[decomp-toolkit](https://github.com/encounter/decomp-toolkit), the
Metrowerks CodeWarrior compilers, and on non-Windows hosts
[wibo](https://github.com/decompals/wibo) or Wine. None of them are
redistributed here.
