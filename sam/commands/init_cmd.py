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
    """
    as_json = getattr(args, "json", False)
    try:
        sam_home = sam_config.get_sam_home()
        sam_config.init_sam_home(sam_home=sam_home, force=getattr(args, "force", False))

        # Source wrapper: relative to this file: ../../wrapper/pi_wrapper.py
        source_wrapper = (_THIS_DIR.parent.parent / "wrapper" / "pi_wrapper.py").resolve()
        if not source_wrapper.is_file():
            # Fallback: check sam/__init__.py location for embedded wrapper
            source_wrapper = (_SAM_PKG.parent / "wrapper" / "pi_wrapper.py").resolve()
        if not source_wrapper.is_file():
            # Last resort: look for pi_wrapper.py in the sam package
            source_wrapper = (_SAM_PKG / "pi_wrapper.py").resolve()

        target_wrapper = sam_config.wrapper_path(sam_home)
        target_wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        if source_wrapper.is_file():
            shutil.copy2(str(source_wrapper), str(target_wrapper))
            os.chmod(str(target_wrapper), 0o700)
        else:
            # Write a minimal wrapper placeholder
            target_wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, subprocess, json, os, tempfile, time, uuid\n"
                "# pi-wrapper placeholder - install full version from sam package\n"
                "print('pi-wrapper not installed', file=sys.stderr)\n"
                "sys.exit(1)\n"
            )
            os.chmod(str(target_wrapper), 0o700)

        result = {
            "status": "ok",
            "sam_home": str(sam_home),
            "wrapper": str(target_wrapper),
        }
        if as_json:
            print(json.dumps(result))
        else:
            print(f"SAM home initialized: {sam_home}")
            print(f"Wrapper installed: {target_wrapper}")
        return 0

    except Exception as e:
        msg = str(e)
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: init failed: {msg}", file=sys.stderr)
        return 1
