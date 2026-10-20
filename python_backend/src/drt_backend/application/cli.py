"""Package diagnostics and static scene inspection."""

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import struct
import sys

from drt_backend import __version__


def _spawn_diagnostic_child(connection):
    """Import-safe zero-ray deployment check, never starts another host/Agent."""
    import os
    connection.send(dict(pid=os.getpid(),parent_pid=os.getppid(),frozen=bool(getattr(sys,'frozen',False)),executable=sys.executable))
    connection.close()


def _spawn_diagnostic():
    import multiprocessing
    from time import monotonic
    from drt_backend.ipc.host import configure_source_workers
    configure_source_workers()
    context=multiprocessing.get_context('spawn');children=[];rows=[];error=None
    try:
        for _ in range(2):
            receive,send=context.Pipe(duplex=False)
            process=context.Process(target=_spawn_diagnostic_child,args=(send,),daemon=True)
            children.append((process,receive));process.start();send.close()
        deadline=monotonic()+8
        for process,receive in children:
            if not receive.poll(max(0,deadline-monotonic())):raise TimeoutError('Frozen spawn diagnostic readiness expired.')
            row=receive.recv();process.join(max(0,deadline-monotonic()))
            if process.is_alive() or process.exitcode!=0 or row['pid']!=process.pid:raise RuntimeError('Spawn diagnostic identity/exit mismatch.')
            rows.append(row|dict(exit_code=process.exitcode))
    except Exception as failure:error=type(failure).__name__+': '+str(failure)
    finally:
        for process,receive in children:
            if process.is_alive():process.terminate();process.join(2)
            receive.close()
    return dict(status='PASS' if error is None else 'FAIL',workers=rows,error=error,numerical_rays=0)


def _image_codec_report() -> dict:
    """Explicit bounded native-codec diagnostic; no scene or disk output."""
    from io import BytesIO
    from PIL import Image

    result = {}
    for format_name in ("PNG", "JPEG"):
        stream = BytesIO()
        with Image.new("RGB", (1, 1), (64, 128, 192)) as fixture:
            fixture.save(stream, format=format_name)
        stream.seek(0)
        with Image.open(stream, formats=(format_name,)) as decoded:
            decoded.load()
            if decoded.size != (1, 1) or decoded.mode != "RGB" or decoded.format != format_name:
                raise RuntimeError(f"{format_name} codec diagnostic returned invalid image metadata.")
            result[format_name] = {"status": "PASS", "width": 1, "height": 1, "mode": "RGB"}
        stream.close()
    return result




def _inspect_scene(args: argparse.Namespace) -> int:
    """Keep numerical imports lazy and boundary errors safe for a process caller."""
    code = 0
    try:
        from drt_backend.raytracing.common.errors import GlbError, GlbFileError, UnsupportedFeatureError
        from drt_backend.raytracing.scene.inspection import inspect_scene

        try:
            inspection = inspect_scene(args.scene, scene_index=args.scene_index).to_dict()
            response = {"schema_version": 1, "kind": "drt.scene_inspection_response", "ok": True,
                        "inspection": inspection}
        except GlbError as error:
            code = 3
            if isinstance(error, GlbFileError):
                error_code, message = "SCENE_FILE_ERROR", "The scene file could not be read."
            elif isinstance(error, UnsupportedFeatureError):
                error_code, message = "SCENE_UNSUPPORTED", "The scene uses an unsupported format or feature."
            else:
                error_code, message = "SCENE_INPUT_ERROR", "The scene contains invalid input data."
            response = {"schema_version": 1, "kind": "drt.scene_inspection_response", "ok": False,
                        "error": {"code": error_code, "message": message}}
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except Exception:
        # Process boundary: unexpected failures are distinct from ordinary input errors.
        code = 4
        response = {"schema_version": 1, "kind": "drt.scene_inspection_response", "ok": False,
                    "error": {"code": "INTERNAL_ERROR", "message": "Scene inspection failed internally."}}
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if args.json_output:
        print(encoded)
    elif code == 0:
        print(f"Scene {inspection['scene_index']}: {inspection['counts']['active']['nodes']} nodes; "
              f"Static scene inputs ready: {inspection['m3_input_ready']}")
    if code:
        print(f"{response['error']['code']}: {response['error']['message']}", file=sys.stderr)
    return code


def environment_report() -> dict:
    """Return fixed diagnostic fields without paths, hostnames or scene access."""
    return {
        "schema_version": 1,
        "package": {
            "name": "drt_backend",
            "version": __version__,
            "version_kind": "release",
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "bits": struct.calcsize("P") * 8,
        },
        "runtime_dependencies": [
            {"name": name, "version": version(name)} for name in ("numpy", "pygltflib", "Pillow")
        ],
        "capabilities": {
            "renderer": "original_room_cpu_pbr_path_tracing",
            "coordinator": "central_task_acceptance",
            "distribution": "local_and_pinned_mutual_tls_remote",
            "frontend_ipc": "original_room_v5",
            "worker_wire": "room_v4",
            "resolutions": [[160,90],[640,360]],
            "default_spp": 1,
            "default_depth": 1,
        },
    }


def main(argv: list[str] | None = None) -> int:
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('agent','--agent'):
        from drt_backend.lan.agent import main as agent_main
        return agent_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="drt-agent",
        description="DRT standalone Linux Worker / CPU PBR backend. Start with agent --help; connecting does not render.",
        epilog="Examples: drt-agent agent --config ./node/agent.json | drt-agent doctor --json --check-spawn | drt-agent probe --agents ./trusted.json. See README.md and docs/USER_GUIDE.md.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} ",
    )
    commands = parser.add_subparsers(dest="command")
    from .worker_cli import add_commands
    add_commands(commands)
    doctor = commands.add_parser("doctor", help="Report the local package environment")
    doctor.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only")
    doctor.add_argument("--check-image-codecs", action="store_true", help="Decode one in-memory 1x1 PNG and JPEG")
    doctor.add_argument('--check-spawn', action='store_true', help='Bounded two-child zero-ray Linux/Windows deployment check')
    inspect = commands.add_parser("inspect-scene", help="Inspect a GLB scene; this is not a render benchmark")
    inspect.add_argument("--scene", required=True, help="Explicit GLB file path")
    inspect.add_argument("--scene-index", type=int, help="Explicit nonnegative scene index")
    inspect.add_argument("--json", action="store_true", dest="json_output", help="Emit one diagnostic response object")
    args = parser.parse_args(argv)
    if hasattr(args,'worker_action'):
        try:return args.worker_action(args)
        except Exception as error:
            print(f'WORKER_CLI_ERROR: {error}',file=sys.stderr);return 2
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "inspect-scene":
        if args.scene_index is not None and args.scene_index < 0:
            parser.error("--scene-index must be nonnegative")
        return _inspect_scene(args)
    try:
        report = environment_report()
        if args.check_image_codecs:
            report["image_codecs"] = _image_codec_report()
        if args.check_spawn:
            report['spawn'] = _spawn_diagnostic()
    except PackageNotFoundError as error:
        print(f"Missing runtime dependency: {error.name}; install the project dependencies.", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        print(f"drt_backend {__version__} ")
        python = report["python"]
        print(f"Python: {python['implementation']} {python['version']} / {python['bits']}-bit")
        for dependency in report["runtime_dependencies"]:
            print(f"Runtime dependency: {dependency['name']} {dependency['version']}")
        for name, state in report["capabilities"].items():
            print(f"{name}: {state}")
    return 1 if report.get('spawn',{}).get('status')=='FAIL' else 0
