"""Select an adapter command interface and forward its arguments unchanged.

Each adapter owns its actions, manifest format, validation and run creation.
Use sugarloaf ACTION --adapter ADAPTER --help for adapter-specific options.
"""

import argparse
import sys
from collections.abc import Sequence
from importlib import import_module

# Entry modules implement main(argv, *, prog) -> int. Add a route when an
# adapter has a concrete command implementation; native logic stays there.
_ADAPTER_COMMANDS = {
    "quantconnect": "sugarloaf.adapters.quantconnect.cloud.backtests",
}


def main(argv: Sequence[str] | None = None) -> int:
    """Select the requested adapter lazily without opening its manifest."""
    # The console entrypoint supplies argv implicitly; passing argv explicitly also
    # allows scripts and tests to use the same routing function.
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="sugarloaf", description=__doc__, add_help=False, allow_abbrev=False,
        usage="%(prog)s ACTION --adapter ADAPTER [adapter options]",
    )
    # This parser owns only adapter selection. The selected adapter defines actions,
    # manifest interpretation and all provider-specific options.
    parser.add_argument("--adapter", required=True, choices=tuple(_ADAPTER_COMMANDS))
    if arguments in (["-h"], ["--help"]):
        parser.print_help()
        return 0
    # Keep unknown options intact: --manifest and run parameters belong to the
    # adapter parser rather than a second shared configuration schema.
    selected, remaining = parser.parse_known_args(arguments)
    # Lazy import keeps general help independent of provider SDKs and credentials.
    # The route names infrastructure only; no private strategy is imported here.
    command = import_module(_ADAPTER_COMMANDS[selected.adapter])
    # The adapter returns a process exit code, while its own command prints results.
    # Forwarding arguments does not execute a node or build a graph locally.
    return command.main(remaining, prog=f"sugarloaf --adapter {selected.adapter}")


if __name__ == "__main__":
    raise SystemExit(main())
