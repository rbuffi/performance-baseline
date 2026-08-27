from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from pyVmomi import vim

from .vcenter import VCenter, VCenterError, as_utc, pick_other

logger = logging.getLogger(__name__)

MIB = 1024 * 1024


@dataclass
class MigrateResult:
    kind: str
    vm: str
    src: str
    dst: str
    duration: float
    rate: float
    completed_at: datetime


def _duration_seconds(task: vim.Task) -> float:
    start = as_utc(task.info.startTime)
    complete = as_utc(task.info.completeTime)
    if start is None or complete is None:
        raise VCenterError("Task is missing startTime or completeTime")
    return max((complete - start).total_seconds(), 0.0)


def _rate_mib_s(size_mib: float, duration: float) -> float:
    if duration <= 0:
        return 0.0
    return size_mib / duration


def resolve_dest_host(
    vcenter: VCenter,
    vm: vim.VirtualMachine,
    dest_host: str | None,
    configured_hosts: list[str],
) -> str:
    current = vm.runtime.host.name
    if dest_host:
        if dest_host.lower() == current.lower():
            raise VCenterError(
                f"Destination host {dest_host} is the current host of {vm.name}"
            )
        return dest_host
    other = pick_other(current, configured_hosts)
    if other is None:
        raise VCenterError(
            f"No destination host for vMotion of {vm.name} "
            f"(current host {current}; configure vmotion.hosts or pass --dest-host)"
        )
    return other


def resolve_dest_datastore(
    vcenter: VCenter,
    vm: vim.VirtualMachine,
    dest_datastore: str | None,
    configured_datastores: list[str],
) -> str:
    current = vcenter.vm_primary_datastore(vm).name
    if dest_datastore:
        if dest_datastore.lower() == current.lower():
            raise VCenterError(
                f"Destination datastore {dest_datastore} is already used by {vm.name}"
            )
        return dest_datastore
    other = pick_other(current, configured_datastores)
    if other is None:
        raise VCenterError(
            f"No destination datastore for storage vMotion of {vm.name} "
            f"(current datastore {current}; configure storage_vmotion.datastores "
            "or pass --dest-datastore)"
        )
    return other


def run_vmotion(
    vcenter: VCenter,
    vm_name: str,
    dest_host_name: str,
    dry_run: bool = False,
) -> MigrateResult:
    vm = vcenter.get_vm(vm_name)
    src_host = vm.runtime.host.name
    dest_host = vcenter.get_host(dest_host_name)
    memory_mib = float(vcenter.memory_mb(vm))

    logger.info(
        "vMotion %s: %s -> %s (memory %.0f MiB)",
        vm_name,
        src_host,
        dest_host.name,
        memory_mib,
    )
    if dry_run:
        return MigrateResult(
            kind="vmotion",
            vm=vm_name,
            src=src_host,
            dst=dest_host.name,
            duration=0.0,
            rate=0.0,
            completed_at=datetime.now(timezone.utc),
        )

    spec = vim.vm.RelocateSpec()
    spec.host = dest_host
    spec.pool = vcenter.host_resource_pool(dest_host)
    task = vm.RelocateVM_Task(spec)
    vcenter.wait_for_task(task)

    duration = _duration_seconds(task)
    completed_at = as_utc(task.info.completeTime) or datetime.now(timezone.utc)
    result = MigrateResult(
        kind="vmotion",
        vm=vm_name,
        src=src_host,
        dst=dest_host.name,
        duration=duration,
        rate=_rate_mib_s(memory_mib, duration),
        completed_at=completed_at,
    )
    logger.info(
        "vMotion finished in %.3fs (%.2f MB/s)", result.duration, result.rate
    )
    return result


def run_storage_vmotion(
    vcenter: VCenter,
    vm_name: str,
    dest_datastore_name: str,
    dry_run: bool = False,
) -> MigrateResult:
    vm = vcenter.get_vm(vm_name)
    src_ds = vcenter.vm_primary_datastore(vm)
    dest_ds = vcenter.get_datastore(dest_datastore_name)
    committed_mib = vcenter.committed_bytes(vm) / MIB

    logger.info(
        "Storage vMotion %s: %s -> %s (committed %.1f MiB)",
        vm_name,
        src_ds.name,
        dest_ds.name,
        committed_mib,
    )
    if dry_run:
        return MigrateResult(
            kind="storage_vmotion",
            vm=vm_name,
            src=src_ds.name,
            dst=dest_ds.name,
            duration=0.0,
            rate=0.0,
            completed_at=datetime.now(timezone.utc),
        )

    spec = vim.vm.RelocateSpec()
    spec.datastore = dest_ds
    spec.disk = vcenter.disk_locators(vm, dest_ds)
    if vm.resourcePool is not None:
        spec.pool = vm.resourcePool
    task = vm.RelocateVM_Task(spec)
    vcenter.wait_for_task(task)

    duration = _duration_seconds(task)
    completed_at = as_utc(task.info.completeTime) or datetime.now(timezone.utc)
    result = MigrateResult(
        kind="storage_vmotion",
        vm=vm_name,
        src=src_ds.name,
        dst=dest_ds.name,
        duration=duration,
        rate=_rate_mib_s(committed_mib, duration),
        completed_at=completed_at,
    )
    logger.info(
        "Storage vMotion finished in %.3fs (%.2f MB/s)",
        result.duration,
        result.rate,
    )
    return result
