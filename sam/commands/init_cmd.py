#!/usr/bin/env python3
"""SAM init_cmd — Initialize SAM home directory tree.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §7
"""

import json
import os
import shutil
import sys
from pathlib import Path

# Add parent dir for sam package access if running as script
_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import config as sam_config


def run(args):
    """Initialize SAM_HOME directory tree.

    Line 1: Assign sam_home = sam.config.get_sam_home()
    Line 2: Call sam.config.init_sam_home(sam_home=sam_home, force=args.force)
    Line 3: Assign source_wrapper = absolute path to sam/../wrapper/pi_wrapper.py
    Line 4: Assign target_wrapper = sam.config.wrapper_path(sam_home)
    Line 5: Copy source_wrapper to target_wrapper using shutil.copy2
    Line 6: Call os.chmod(target_wrapper, 0o700) to ensure it is executable
    Line 7: Print JSON success output containing sam_home path. Return 0.

    Installs one wrapper per harness in sam.config.HARNESS_WRAPPERS
    (pi-wrapper, agy-wrapper, opencode-wrapper from wrapper/*.py). A harness whose source file is missing is
    skipped gracefully — except pi, which falls back to a placeholder.
    """
    as_json = getattr(args, "json", False)
    try:
        sam_home = sam_config.get_sam_home()
        sam_config.init_sam_home(sam_home=sam_home, force=getattr(args, "force", False))

        wrapper_dir = (_THIS_DIR.parent.parent / "wrapper").resolve()
        if not wrapper_dir.is_dir():
            wrapper_dir = (_SAM_PKG.parent / "wrapper").resolve()
        # source filename per installed basename
        source_names = {"pi-wrapper": "pi_wrapper.py", "agy-wrapper": "agy_wrapper.py", "opencode-wrapper": "opencode_wrapper.py"}
        installed = []
        missing = []
        for _harness, _basename in sam_config.HARNESS_WRAPPERS.items():
            source_wrapper = wrapper_dir / source_names[_basename]
            if not source_wrapper.is_file():
                # Last resort: look inside the sam package itself
                source_wrapper = (_SAM_PKG / source_names[_basename]).resolve()
            target_wrapper = sam_config.wrapper_path(sam_home, _harness)
            target_wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

            if source_wrapper.is_file():
                shutil.copy2(str(source_wrapper), str(target_wrapper))
                os.chmod(str(target_wrapper), 0o700)
                installed.append(str(target_wrapper))
            elif _basename == sam_config.WRAPPER_FILENAME:
                # Write a minimal wrapper placeholder (pi only; others skip gracefully)
                target_wrapper.write_text(
                    "#!/usr/bin/env python3\n"
                    "import sys, subprocess, json, os, tempfile, time, uuid\n"
                    "# pi-wrapper placeholder - install full version from sam package\n"
                    "print('pi-wrapper not installed', file=sys.stderr)\n"
                    "sys.exit(1)\n"
                )
                os.chmod(str(target_wrapper), 0o700)
                installed.append(str(target_wrapper))
            else:
                missing.append(_basename)

        # Install sam-tui dashboard (resolved states, archived hidden by
        # default) from wrapper/sam-tui.py to bin/sam-tui.
        tui_source = wrapper_dir / "sam-tui.py"
        if not tui_source.is_file():
            tui_source = (_SAM_PKG / "tui.py").resolve()
        if tui_source.is_file():
            tui_target = sam_config.bin_dir(sam_home) / "sam-tui"
            shutil.copy2(str(tui_source), str(tui_target))
            os.chmod(str(tui_target), 0o755)
            installed.append(str(tui_target))

        target_wrapper = sam_config.wrapper_path(sam_home)
        result = {
            "status": "ok",
            "sam_home": str(sam_home),
            "wrapper": str(target_wrapper),
            "wrappers": installed,
            "missing_wrappers": missing,
        }
        if as_json:
            print(json.dumps(result))
        else:
            print(f"SAM home initialized: {sam_home}")
            print(f"Wrapper installed: {target_wrapper}")
            for name in missing:
                print(f"Warning: wrapper source missing for {name}; that harness is unavailable",
                      file=sys.stderr)
        return 0

    except Exception as e:
        msg = str(e)
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: init failed: {msg}", file=sys.stderr)
        return 1
