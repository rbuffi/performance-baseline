from __future__ import annotations

import argparse
import logging
import sys
from argparse import Namespace

from . import __version__
from .config import AppConfig, ConfigError, load_config
from .influx import InfluxWriter
from .tests_boot import run_boot
from .tests_migrate import (
    resolve_dest_datastore,
    resolve_dest_host,
    run_storage_vmotion,
    run_vmotion,
)
from .vcenter import VCenter

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m perfbaseline",
        description=(
            "Run VMware performance baseline tests and write results to InfluxDB 1.x."
        ),
        epilog=(
            "examples:\n"
            "  python -m perfbaseline --svmotion\n"
            "  python -m perfbaseline --vmotion --dest-host esxi-b.example.com\n"
            "  python -m perfbaseline --boot\n"
            "  python -m perfbaseline --svmotion --vmotion --boot\n"
            "  python -m perfbaseline --dry-run --svmotion\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config (default: config.yaml)",
    )
    parser.add_argument(
        "--svmotion",
        action="store_true",
        help="Run a storage vMotion and write duration + rate",
    )
    parser.add_argument(
        "--vmotion",
        action="store_true",
        help="Run a compute vMotion and write duration + rate",
    )
    parser.add_argument(
        "--boot",
        action="store_true",
        help="Read last starttime/osstarttime from vCenter (no power-cycle)",
    )
    parser.add_argument("--vm", help="Override the VM name from config")
    parser.add_argument(
        "--dest-host",
        help="Destination ESXi host for vMotion (otherwise ping-pong from config)",
    )
    parser.add_argument(
        "--dest-datastore",
        help="Destination datastore for storage vMotion (otherwise ping-pong from config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate objects and print the plan; do not migrate or write to InfluxDB",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.svmotion or args.vmotion or args.boot):
        parser.error("Specify at least one of --svmotion --vmotion --boot")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    if args.vm:
        cfg.vm = args.vm

    failures = 0
    try:
        with VCenter(
            host=cfg.vcenter.host,
            username=cfg.vcenter.username,
            password=cfg.vcenter.password,
            port=cfg.vcenter.port,
            insecure=cfg.vcenter.insecure,
        ) as vcenter:
            writer: InfluxWriter | None = None
            if not args.dry_run:
                writer = InfluxWriter(cfg.influxdb)
                writer.connect()
            try:
                if args.svmotion:
                    try:
                        _run_svmotion(vcenter, writer, cfg, args)
                    except Exception as exc:
                        failures += 1
                        logger.error("Storage vMotion failed: %s", exc)
                if args.vmotion:
                    try:
                        _run_vmotion(vcenter, writer, cfg, args)
                    except Exception as exc:
                        failures += 1
                        logger.error("vMotion failed: %s", exc)
                if args.boot:
                    try:
                        _run_boot(vcenter, writer, cfg, args)
                    except Exception as exc:
                        failures += 1
                        logger.error("Boot test failed: %s", exc)
            finally:
                if writer is not None:
                    writer.close()
    except Exception as exc:
        logger.error("vCenter connection failed: %s", exc)
        return 1

    if failures:
        return 1
    return 0


def _run_svmotion(
    vcenter: VCenter,
    writer: InfluxWriter | None,
    cfg: AppConfig,
    args: Namespace,
) -> None:
    vm = vcenter.get_vm(cfg.vm)
    dest = resolve_dest_datastore(
        vcenter, vm, args.dest_datastore, cfg.svmotion_datastores
    )
    result = run_storage_vmotion(vcenter, cfg.vm, dest, dry_run=args.dry_run)
    if args.dry_run:
        logger.info(
            "dry-run storage vMotion %s: %s -> %s", result.vm, result.src, result.dst
        )
        return
    assert writer is not None
    writer.write(
        measurement="storage_vmotion",
        tags={
            "vm": result.vm,
            "src_datastore": result.src,
            "dst_datastore": result.dst,
            "vcenter": cfg.vcenter.host,
        },
        fields={"duration": result.duration, "rate": result.rate},
        time=result.completed_at,
    )


def _run_vmotion(
    vcenter: VCenter,
    writer: InfluxWriter | None,
    cfg: AppConfig,
    args: Namespace,
) -> None:
    vm = vcenter.get_vm(cfg.vm)
    dest = resolve_dest_host(vcenter, vm, args.dest_host, cfg.vmotion_hosts)
    result = run_vmotion(vcenter, cfg.vm, dest, dry_run=args.dry_run)
    if args.dry_run:
        logger.info("dry-run vMotion %s: %s -> %s", result.vm, result.src, result.dst)
        return
    assert writer is not None
    writer.write(
        measurement="vmotion",
        tags={
            "vm": result.vm,
            "src_host": result.src,
            "dst_host": result.dst,
            "vcenter": cfg.vcenter.host,
        },
        fields={"duration": result.duration, "rate": result.rate},
        time=result.completed_at,
    )


def _run_boot(
    vcenter: VCenter,
    writer: InfluxWriter | None,
    cfg: AppConfig,
    args: Namespace,
) -> None:
    result = run_boot(
        vcenter,
        cfg.vm,
        lookback_hours=cfg.boot_lookback_hours,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        logger.info(
            "dry-run boot %s: starttime=%.3fs osstarttime=%.3fs",
            result.vm,
            result.starttime,
            result.osstarttime,
        )
        return
    assert writer is not None
    writer.write(
        measurement="vm_boot",
        tags={
            "vm": result.vm,
            "host": result.host,
            "vcenter": cfg.vcenter.host,
        },
        fields={"starttime": result.starttime, "osstarttime": result.osstarttime},
        time=result.queried_at,
    )


if __name__ == "__main__":
    sys.exit(main())
