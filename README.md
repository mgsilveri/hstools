# hstools - Blender Addons & Scripts

Blender addons and startup scripts for the art pipeline.

## Repository Structure

```
hstools/
├── addons/
│   ├── modokit/        ← Modo-style selection, transforms and UV tools
│   │   ├── blender_manifest.toml
│   │   ├── __init__.py
│   │   └── ...
│   └── your_addon/     ← drop new addons here
│       ├── __init__.py
│       └── ...
├── startup/            ← scripts that run automatically on every Blender launch
│   └── your_script.py
└── deploy.bat          ← deploy everything to the network share
```

## Adding a New Addon

1. Create a subfolder under `addons/` (e.g. `addons/myaddon/`)
2. Add an `__init__.py` with a `bl_info` dict (and optionally `blender_manifest.toml`)
3. That is it - picked up automatically by Blender and synced by `deploy.bat`

## Adding a Startup Script

1. Drop your `.py` file into `startup/`
2. It must have `register()` and `unregister()` functions (required by Blender 5.0+)
3. Blender will run it automatically on every launch

## Development Setup (your machine)

1. Clone this repo anywhere (e.g. `C:\Users\hector.silveri\Dev\hstools\`)
2. In Blender: **Preferences -> File Paths -> Script Directories -> Add** -> point to the repo root
3. All addons and startup scripts are active immediately after restarting Blender

## Deploying Updates to Other Users

1. Make your changes and commit/push to GitHub
2. Run `deploy.bat` - it mirrors the full repo to `X:\Temp\hector.silveri\blender_script\`

## First-Time Setup (other users)

1. In Blender: **Preferences -> File Paths -> Script Directories -> Add**
2. Enter: `X:\Temp\hector.silveri\blender_script`
3. Restart Blender - all addons and startup scripts will be active

From then on, whenever `deploy.bat` is run, they get the latest version on next Blender restart.

## Code Style

- All functions require type annotations
- All public functions and classes require docstrings
- Relative imports for all intra-package imports (required by Blender's addon system)
