from __future__ import annotations

import logging
import ssl
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim

logger = logging.getLogger(__name__)

T = TypeVar("T")


class VCenterError(Exception):
    """vCenter lookup or operation failed."""


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def pick_other(current: str, candidates: list[str]) -> str | None:
    """Return the first candidate that does not match the current name."""
    current_l = current.lower()
    for name in candidates:
        if name.lower() != current_l:
            return name
    return None


class VCenter:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        insecure: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.insecure = insecure
        self.si: Any = None
        self.content: Any = None
        self._counters: dict[tuple[str, str, str], int] | None = None

    def connect(self) -> None:
        ssl_context = None
        if self.insecure:
            ssl_context = ssl._create_unverified_context()
        logger.info("Connecting to vCenter %s:%s", self.host, self.port)
        self.si = SmartConnect(
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=self.port,
            sslContext=ssl_context,
        )
        self.content = self.si.RetrieveContent()

    def disconnect(self) -> None:
        if self.si is not None:
            Disconnect(self.si)
            self.si = None
            self.content = None

    def __enter__(self) -> VCenter:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()

    def current_time(self) -> datetime:
        return as_utc(self.si.CurrentTime()) or datetime.now(timezone.utc)

    def get_obj(self, vimtype: list[type[T]], name: str) -> T:
        container = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, vimtype, True
        )
        try:
            matches = [obj for obj in container.view if obj.name == name]
        finally:
            container.Destroy()
        if not matches:
            type_name = vimtype[0].__name__ if vimtype else "ManagedObject"
            raise VCenterError(f"{type_name} not found: {name}")
        if len(matches) > 1:
            logger.warning("Multiple objects named %r; using the first match", name)
        return matches[0]

    def get_vm(self, name: str) -> vim.VirtualMachine:
        return self.get_obj([vim.VirtualMachine], name)

    def get_host(self, name: str) -> vim.HostSystem:
        return self.get_obj([vim.HostSystem], name)

    def get_datastore(self, name: str) -> vim.Datastore:
        return self.get_obj([vim.Datastore], name)

    def wait_for_task(self, task: vim.Task) -> vim.Task:
        try:
            WaitForTask(task, raiseOnError=True, si=self.si)
        except Exception as exc:
            raise VCenterError(f"vCenter task failed: {exc}") from exc
        return task

    def host_resource_pool(self, host: vim.HostSystem) -> vim.ResourcePool:
        parent = host.parent
        pool = getattr(parent, "resourcePool", None)
        if pool is None:
            raise VCenterError(
                f"Could not determine resource pool for host {host.name}"
            )
        return pool

    def vm_primary_datastore(self, vm: vim.VirtualMachine) -> vim.Datastore:
        for device in vm.config.hardware.device:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            backing = device.backing
            datastore = getattr(backing, "datastore", None)
            if datastore is not None:
                return datastore
        if vm.datastore:
            return vm.datastore[0]
        raise VCenterError(f"VM {vm.name} has no datastore")

    def committed_bytes(self, vm: vim.VirtualMachine) -> int:
        storage = vm.summary.storage
        if storage is not None and storage.committed is not None:
            return int(storage.committed)
        total = 0
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                total += int(device.capacityInBytes or 0)
        return total

    def memory_mb(self, vm: vim.VirtualMachine) -> int:
        return int(vm.config.hardware.memoryMB)

    def disk_locators(
        self, vm: vim.VirtualMachine, datastore: vim.Datastore
    ) -> list[vim.vm.RelocateSpec.DiskLocator]:
        locators: list[vim.vm.RelocateSpec.DiskLocator] = []
        for device in vm.config.hardware.device:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            backing = device.backing
            if isinstance(
                backing, vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo
            ):
                raise VCenterError(
                    f"VM {vm.name} has an RDM disk; storage vMotion test is not supported"
                )
            locator = vim.vm.RelocateSpec.DiskLocator()
            locator.diskId = device.key
            locator.datastore = datastore
            locators.append(locator)
        return locators

    def query_events(
        self,
        vm: vim.VirtualMachine,
        event_type_ids: list[str],
        begin_time: datetime,
    ) -> list[vim.event.Event]:
        event_manager = self.content.eventManager
        spec = vim.event.EventFilterSpec()
        spec.entity = vim.event.EventFilterSpec.ByEntity(
            entity=vm,
            recursion=vim.event.EventFilterSpec.RecursionOption.self,
        )
        spec.eventTypeId = event_type_ids
        spec.time = vim.event.EventFilterSpec.ByTime(beginTime=begin_time)
        events = event_manager.QueryEvents(spec) or []
        return list(events)

    def iter_recent_tasks(
        self,
        vm: vim.VirtualMachine,
        begin_time: datetime,
    ) -> Iterator[vim.TaskInfo]:
        task_manager = self.content.taskManager
        spec = vim.TaskFilterSpec()
        spec.entity = vim.TaskFilterSpec.ByEntity(
            entity=vm,
            recursion=vim.TaskFilterSpec.RecursionOption.self,
        )
        spec.time = vim.TaskFilterSpec.ByTime(beginTime=begin_time)
        spec.state = [vim.TaskInfo.State.success]
        collector = task_manager.CreateCollectorForTasks(spec)
        try:
            collector.RewindCollector()
            while True:
                batch = collector.ReadNextTasks(100)
                if not batch:
                    break
                yield from batch
        finally:
            collector.DestroyCollector()

    def last_power_on_task_duration(
        self, vm: vim.VirtualMachine, begin_time: datetime
    ) -> float | None:
        power_tasks = [
            info
            for info in self.iter_recent_tasks(vm, begin_time)
            if (info.descriptionId or "").lower().endswith("poweron")
        ]
        if not power_tasks:
            return None
        latest = max(power_tasks, key=lambda info: info.startTime)
        start = as_utc(latest.startTime)
        complete = as_utc(latest.completeTime)
        if start is None or complete is None:
            return None
        return (complete - start).total_seconds()

    def _load_counters(self) -> dict[tuple[str, str, str], int]:
        if self._counters is not None:
            return self._counters
        counters: dict[tuple[str, str, str], int] = {}
        for counter in self.content.perfManager.perfCounter:
            rollup = str(counter.rollupType).rsplit(".", maxsplit=1)[-1].lower()
            counters[(counter.groupInfo.key, counter.nameInfo.key, rollup)] = (
                counter.key
            )
        self._counters = counters
        return counters

    def counter_id(self, group: str, name: str) -> int:
        counters = self._load_counters()
        for rollup in ("latest", "average", "summation"):
            cid = counters.get((group, name, rollup))
            if cid is not None:
                return cid
        raise VCenterError(f"Performance counter {group}.{name} not found")

    def query_latest_metric(
        self, vm: vim.VirtualMachine, group: str, name: str
    ) -> tuple[datetime, float]:
        perf_manager = self.content.perfManager
        counter_id = self.counter_id(group, name)
        metric_id = vim.PerformanceManager.MetricId(
            counterId=counter_id, instance=""
        )
        summary = perf_manager.QueryPerfProviderSummary(entity=vm)
        interval_id = getattr(summary, "refreshRate", None) or 20
        if interval_id < 0:
            interval_id = 300
        query_spec = vim.PerformanceManager.QuerySpec(
            entity=vm,
            metricId=[metric_id],
            intervalId=interval_id,
            maxSample=1,
        )
        results = perf_manager.QueryPerf(querySpec=[query_spec])
        if not results or not results[0].value or not results[0].value[0].value:
            # Fall back to the 5-minute historical interval.
            query_spec.intervalId = 300
            query_spec.startTime = self.current_time() - timedelta(minutes=30)
            results = perf_manager.QueryPerf(querySpec=[query_spec])
        if not results or not results[0].value or not results[0].value[0].value:
            raise VCenterError(
                f"No samples for {group}.{name} on VM {vm.name}. "
                "VMware Tools and performance counters must be available."
            )
        sample = results[0]
        timestamp = as_utc(sample.sampleInfo[-1].timestamp)
        if timestamp is None:
            timestamp = self.current_time()
        return timestamp, float(sample.value[0].value[-1])
