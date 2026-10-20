"""Import-safe frozen entry shared by Desktop backend and standalone Agent.

PyInstaller's freeze_support must run before argument dispatch: spawned children
use --multiprocessing-fork and must never start another host/listener.
"""

import sys

if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    mode = sys.argv[1] if len(sys.argv)>1 else None
    if mode in ('--ipc', '--agent', '--room-remote'):
        del sys.argv[1]
        if mode == '--ipc':
            from drt_backend.ipc.host import main
        elif mode == '--room-remote':
            from drt_backend.room_pbr.remote import main
        else:
            from drt_backend.lan.agent import main
    else:
        from drt_backend.application.cli import main
    raise SystemExit(main())
