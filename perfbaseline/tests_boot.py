from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pyVmomi import vim

from .vcenter import VCenter, VCenterError, as_utc

logger = logging.getLogger(__name__)

POWER_ON_EVENT_TYPES = [
    "VmStartingEvent",
    "VmPoweredOnEvent",
    "DrsVmPoweredOnEvent",
    "VmRestartedOnAlternateHostEvent",
]


@dataclass
class BootResult:
    vm: str
    host: str
    starttime: float
    osstarttime: float
    queried_at: datetime


def _event_name(event: object) -> str:
    return type(event).__name__


def _is_powered_on_event(event: vim.event.Event) -> bool:
    if isinstance(event, vim.event.VmPoweredOnEvent):
        return True
    name = _event_name(event)
    event_type_id = getattr(event, "eventTypeId", "") or ""
    return name in {
        "VmPoweredOnEvent",
        "DrsVmPoweredOnEvent",
        "VmRestartedOnAlternateHostEvent",
    } or "PoweredOn" in event_type_id


def _is_starting_event(event: vim.event.Event) -> bool:
    if isinstance(event, vim.event.VmStartingEvent):
        return True
    name = _event_name(event)
    event_type_id = getattr(event, "eventTypeId", "") or ""
    return name == "VmStartingEvent" or "VmStarting" in event_type_id


def starttime_from_events(events: list[vim.event.Event]) -> float | None:
    """Duration from last VmStartingEvent to the following VmPoweredOnEvent."""
    if not events:
        return None
    ordered = sorted(events, key=lambda event: event.createdTime)
    last_powered_on: vim.event.Event | None = None
    for event in reversed(ordered):
        if _is_powered_on_event(event):
            last_powered_on = event
            break
    if last_powered_on is None:
        return None

    powered_on_at = as_utc(last_powered_on.createdTime)
    last_starting: vim.event.Event | None = None
    for event in ordered:
        created = as_utc(event.createdTime)
        if created is None or powered_on_at is None or created > powered_on_at:
            continue
        if _is_starting_event(event):
            last_starting = event

    if last_starting is None or powered_on_at is None:
        return None
    started_at = as_utc(last_starting.createdTime)
    if started_at is None:
        return None
    return max((powered_on_at - started_at).total_seconds(), 0.0)


def run_boot(
    vcenter: VCenter,
    vm_name: str,
    lookback_hours: int,
    dry_run: bool = False,
) -> BootResult:
    vm = vcenter.get_vm(vm_name)
    host_name = vm.runtime.host.name if vm.runtime.host else ""
    boot_time = as_utc(vm.runtime.bootTime)
    if boot_time is None:
        raise VCenterError(
            f"VM {vm_name} has no runtime.bootTime (powered off or never started)"
        )

    begin_time = vcenter.current_time() - timedelta(hours=lookback_hours)
    events = vcenter.query_events(vm, POWER_ON_EVENT_TYPES, begin_time)
    starttime = starttime_from_events(events)
    if starttime is None:
        logger.info("No power-on events in lookback window; trying PowerOnVM_Task")
        starttime = vcenter.last_power_on_task_duration(vm, begin_time)
    if starttime is None:
        raise VCenterError(
            f"Could not determine starttime for {vm_name} from vCenter events "
            f"or tasks in the last {lookback_hours}h. Increase boot.event_lookback_hours."
        )

    sample_ts, os_uptime = vcenter.query_latest_metric(vm, "sys", "osUptime")
    os_boot_time = sample_ts - timedelta(seconds=os_uptime)
    osstarttime = (os_boot_time - boot_time).total_seconds()
    if osstarttime < 0:
        logger.warning(
            "osstarttime is negative (%.3fs); VMware Tools/clock skew is likely. Clamping to 0.",
            osstarttime,
        )
        osstarttime = 0.0
    elif osstarttime > 600:
        logger.warning(
            "osstarttime is %.1fs; the guest OS may have rebooted without a VM power-on.",
            osstarttime,
        )

    queried_at = datetime.now(timezone.utc)
    logger.info(
        "Boot metrics for %s: starttime=%.3fs osstarttime=%.3fs (dry_run=%s)",
        vm_name,
        starttime,
        osstarttime,
        dry_run,
    )
    return BootResult(
        vm=vm_name,
        host=host_name,
        starttime=starttime,
        osstarttime=osstarttime,
        queried_at=queried_at,
    )
