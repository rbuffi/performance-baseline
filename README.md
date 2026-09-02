# VMware performance-baseline naar InfluxDB 1

CLI die Storage vMotion en vMotion uitvoert, boot-tijden uit vCenter haalt, en de metingen naar **InfluxDB 1.x** schrijft.

## Vereisten

- Python 3.9+
- `pyvmomi` 8.x (vSphere 7/8 API)
- vCenter met een test-VM, twee ESXi-hosts (vMotion) en twee datastores (Storage vMotion)
- InfluxDB 1.x database (wordt niet automatisch aangemaakt)
- VMware Tools op de test-VM (nodig voor `osstarttime`)

## Installatie

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Vul `config.yaml` met vCenter/Influx-hosts, VM-naam, hosts en datastores. Zet wachtwoorden in `.env`:

```
VCENTER_PASSWORD=...
INFLUX_PASSWORD=...
```

## Gebruik

Zet in `config.yaml` welke tests moeten draaien. Daarna volstaat:

```bash
python -m perfbaseline
```

```yaml
tests:
  - svmotion
  - vmotion
  - boot
```

CLI-flags overschrijven de config (handig om één test te forceren):

```bash
python -m perfbaseline --svmotion
python -m perfbaseline --vmotion --dest-host esxi-b.example.com
python -m perfbaseline --boot
python -m perfbaseline --svmotion --vmotion --boot
python -m perfbaseline --dry-run --svmotion
```

Zonder `--dest-host` / `--dest-datastore` kiest het script de andere host of datastore uit de config (ping-pong).

`--boot` zet de VM **niet** uit. Het leest de laatste power-on uit vCenter-events (`VmStartingEvent` → `VmPoweredOnEvent`, fallback: `PowerOnVM_Task`) en `osstarttime` uit `runtime.bootTime` versus performance counter `sys.osUptime.latest`.

## InfluxDB measurements

| Measurement       | Tags                                         | Fields                         |
|-------------------|----------------------------------------------|--------------------------------|
| `storage_vmotion` | `vm`, `src_datastore`, `dst_datastore`, `vcenter` | `duration` (s), `rate` (MB/s) |
| `vmotion`         | `vm`, `src_host`, `dst_host`, `vcenter`      | `duration` (s), `rate` (MB/s)  |
| `vm_boot`         | `vm`, `host`, `vcenter`                      | `starttime` (s), `osstarttime` (s) |

`rate` is MiB/s: committed storage / duration voor Storage vMotion, geconfigureerd geheugen / duration voor vMotion.

`osstarttime` is de tijd van VM-power-on tot OS-kernel start (firmware/bootloader), niet tot “login-ready”. Als de guest OS later opnieuw is geboot zonder VM power-cycle kan deze waarde te hoog zijn.

## Config en environment

| Variabele            | Doel                          |
|----------------------|-------------------------------|
| `VCENTER_PASSWORD`   | vCenter-wachtwoord            |
| `VCENTER_HOST`       | override `vcenter.host`       |
| `VCENTER_USERNAME`   | override `vcenter.username`   |
| `INFLUX_PASSWORD`    | InfluxDB-wachtwoord           |
| `INFLUX_HOST`        | override `influxdb.host`      |
| `INFLUX_DATABASE`    | override `influxdb.database`  |
| `PERF_VM`            | override `vm`                 |
| `PERF_TESTS`         | override `tests` (comma-separated: `svmotion,vmotion,boot`) |
| `CONFIG_PATH`        | pad naar YAML-config          |

vCenter-account heeft o.a. nodig: Resource.Migrate / Relocate, plus read-rechten op VM, hosts, datastores, events en performance.
