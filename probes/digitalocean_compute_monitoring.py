# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for the endpoints a droplet's health()/metrics() will call.

specs/driver_observability.md's "Out of scope" says implementing either
method for any driver needs its own probe session, "the endpoints
metrics() calls are frequently ones no existing driver has probed". That
is exactly the case here: every existing transcript is of /v2/droplets
and /v2/firewalls, and nothing has ever asked /v2/monitoring anything.

The questions are about a surface with three documented traps and one
undocumented one:

  - cpu returns a *mode*-labelled family, not a single series, and its
    values are cumulative -- so whether it is a COUNTER under this
    project's counter-honesty rule depends on whether DO resets it, which
    the schema does not say.
  - every series carries a host_id label, which is identity aiform's
    output already prints beside the samples.
  - the monitoring endpoints need the droplet agent, so a droplet with
    monitoring disabled -- or one that booted a minute ago -- is the
    realistic case, not the exception.
  - values arrive as JSON *strings*, and the series is a time range
    rather than a reading, so "how stale is the newest point" decides
    whether a number this command prints can be trusted at all.

Read-only by default: every question but one is a GET, and the exception
is the no-agent case, which needs a droplet that genuinely has no agent.

Run:  python probes/digitalocean_compute_monitoring.py --dry-run
      python probes/digitalocean_compute_monitoring.py            # GETs only
      python probes/digitalocean_compute_monitoring.py --mutate   # + throwaway droplet
      python probes/digitalocean_compute_monitoring.py --sweep --mutate
"""

import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe import (  # noqa: E402
    SWEEP_MIN_AGE_MINUTES,
    Probe,
    base_arg_parser,
    unique_name,
)

SESSION = "digitalocean_compute_monitoring"
DROPLET_PREFIX = "aiform-probe-monitoring"
SYSTEM_TEST_TAG = "aiform-system-test"

REGION = "sfo3"
SIZE = "s-1vcpu-512mb-10gb"
IMAGE = "ubuntu-24-04-x64"

# Every metric endpoint specs/driver_observability.md could plausibly
# want. Probed as a set rather than one at a time because the question
# "which of these exist, and which are single-series" is one question.
METRICS = (
    "cpu",
    "memory_total",
    "memory_available",
    "memory_free",
    "memory_cached",
    "filesystem_free",
    "filesystem_size",
    "load_1",
    "load_5",
    "load_15",
    "bandwidth",
)


def _window(minutes: int = 10) -> tuple[int, int]:
    now = int(time.time())
    return now - minutes * 60, now


def _metric_path(metric: str, host_id: str, *, start: int | None = None, end: int | None = None):
    path = f"/monitoring/metrics/droplet/{metric}?host_id={host_id}"
    if start is not None:
        path += f"&start={start}&end={end}"
    return path


def _summarise(body) -> str:
    if not isinstance(body, dict):
        return f"(non-dict body: {str(body)[:80]})"
    data = body.get("data") or {}
    result = data.get("result") or []
    if not result:
        return f"status={body.get('status')} resultType={data.get('resultType')} series=0"
    labels = sorted(result[0].get("metric", {}))
    newest = result[0].get("values", [[None, None]])[-1]
    return (
        f"status={body.get('status')} resultType={data.get('resultType')} "
        f"series={len(result)} labels={labels} newest={newest}"
    )


def run(probe: Probe) -> None:
    host_id = probe.reference_droplet_id

    # --- 1. What shape does a metric come back in at all? -------------
    start, end = _window()
    r = probe.call(
        "GET",
        _metric_path("memory_total", host_id, start=start, end=end),
        note="memory_total on an agent-reporting droplet",
        predict={
            "status": 200,
            "shape": "Prometheus-style matrix: data.result[].values[][ts, string]",
            "series": 1,
            "labels": ["host_id"],
        },
    )
    print("    " + _summarise(r.body))

    # --- 2. Is cpu one series or a labelled family? -------------------
    # The whole reason metrics() cannot just map endpoint -> Sample.
    r = probe.call(
        "GET",
        _metric_path("cpu", host_id, start=start, end=end),
        note="cpu returns one series per mode, not one series",
        predict={
            "status": 200,
            "series": 8,
            "labels": ["host_id", "mode"],
            "note": "cumulative seconds per mode; monotonic only between reboots",
        },
    )
    print("    " + _summarise(r.body))
    if isinstance(r.body, dict):
        modes = sorted(
            s["metric"].get("mode") for s in (r.body.get("data") or {}).get("result") or []
        )
        print(f"    modes: {modes}")

    # --- 3. Which of the documented metrics actually answer? ----------
    for metric in METRICS:
        if metric in ("memory_total", "cpu"):
            continue
        r = probe.call(
            "GET",
            _metric_path(metric, host_id, start=start, end=end),
            note=f"{metric} shape and cardinality",
            predict={"status": 200},
        )
        print("    " + _summarise(r.body))

    # --- 4. How stale is the newest point? ---------------------------
    # Decides whether a number this command prints is a reading or a
    # several-minute-old average. specs/driver_observability.md asserts
    # the latter as *recalled, not verified*.
    r = probe.call(
        "GET",
        _metric_path("memory_available", host_id, start=start, end=end),
        note="staleness of the newest data point",
        predict={"status": 200, "newest_age_seconds": "under 120"},
    )
    if isinstance(r.body, dict):
        result = (r.body.get("data") or {}).get("result") or []
        if result:
            newest_ts = result[0]["values"][-1][0]
            step = None
            values = result[0]["values"]
            if len(values) > 1:
                step = values[-1][0] - values[-2][0]
            print(f"    newest point is {int(time.time()) - newest_ts}s old; step={step}s")

    # --- 5. Are start/end required? ----------------------------------
    r = probe.call(
        "GET",
        _metric_path("memory_total", host_id),
        note="omitting start and end",
        predict={"status": 400, "note": "schema marks both required"},
    )
    print(f"    body: {str(r.body)[:200]}")

    # --- 6. A host_id that is not a droplet --------------------------
    # health()/metrics() must tell "gone" apart from "broken", and the
    # compute driver's read() maps 404 to ResourceNotFoundError.
    r = probe.call(
        "GET",
        _metric_path("memory_total", "999999999", start=start, end=end),
        note="a host_id that does not exist",
        predict={"status": 404, "note": "matching GET /v2/droplets/{id} on a missing droplet"},
    )
    print(f"    body: {str(r.body)[:200]}")

    # --- 7. A malformed host_id --------------------------------------
    r = probe.call(
        "GET",
        _metric_path("memory_total", "not-an-id", start=start, end=end),
        note="a malformed host_id",
        predict={"status": 400},
    )
    print(f"    body: {str(r.body)[:200]}")

    # --- 8. An unknown metric name -----------------------------------
    r = probe.call(
        "GET",
        _metric_path("cpu_percent", host_id, start=start, end=end),
        note="a metric name DigitalOcean does not define",
        predict={"status": 404},
    )
    print(f"    body: {str(r.body)[:200]}")

    # --- 8b. bandwidth, with the two parameters its 400 implied ------
    # The 400 above is the finding: bandwidth is the only metric in the
    # family that is not addressable by host_id alone, so a driver
    # looping over metric names uniformly gets one 400 for free.
    for interface in ("public", "private"):
        for direction in ("inbound", "outbound"):
            r = probe.call(
                "GET",
                _metric_path("bandwidth", host_id, start=start, end=end)
                + f"&interface={interface}&direction={direction}",
                note=f"bandwidth {interface} {direction}",
                predict={"status": 200, "labels": ["direction", "host_id", "interface"]},
            )
            print("    " + _summarise(r.body))

    # --- 9. What does the droplet GET offer health()? ----------------
    # health() may delegate to read(), but read() projects status away --
    # so this asks what the raw droplet object carries that a verdict
    # could be built from.
    r = probe.call(
        "GET",
        f"/droplets/{host_id}",
        note="fields a health verdict can be built from",
        predict={
            "status": 200,
            "fields": ["status", "locked", "features", "networks"],
            "statuses": "new|active|off|archive",
        },
    )
    if isinstance(r.body, dict):
        d = r.body["droplet"]
        print(
            f"    status={d['status']} locked={d.get('locked')} "
            f"features={d.get('features')} v4={len(d.get('networks', {}).get('v4', []))}"
        )

    if not probe.mutate:
        print("\n  -- skipping the no-agent case: needs --mutate to create a droplet --")
        return

    # --- 10. The realistic case: a droplet with no agent -------------
    # A droplet created with monitoring: false can never report, and one
    # created a minute ago has not reported yet. Both are what an
    # operator will actually hit, and neither is an error.
    name = unique_name(DROPLET_PREFIX)
    r = probe.call(
        "POST",
        "/droplets",
        {
            "name": name,
            "region": REGION,
            "size": SIZE,
            "image": IMAGE,
            "monitoring": False,
            "tags": [SYSTEM_TEST_TAG],
        },
        note="create a droplet with monitoring disabled",
        predict={"status": 202},
    )
    if r.status not in (201, 202) or not isinstance(r.body, dict):
        print("    could not create the throwaway droplet; skipping the no-agent probes")
        return
    new_id = r.body["droplet"]["id"]
    probe.cleanup("DELETE", f"/droplets/{new_id}")

    start, end = _window()
    r = probe.call(
        "GET",
        _metric_path("memory_total", str(new_id), start=start, end=end),
        note="a metric for a droplet whose agent has never reported",
        predict={
            "status": 200,
            "series": 0,
            "note": "success with an empty result, not an error -- nothing has been pushed",
        },
    )
    print("    " + _summarise(r.body))

    r = probe.call(
        "GET",
        _metric_path("cpu", str(new_id), start=start, end=end),
        note="cpu for a droplet whose agent has never reported",
        predict={"status": 200, "series": 0},
    )
    print("    " + _summarise(r.body))

    r = probe.call(
        "GET",
        f"/droplets/{new_id}",
        note="a freshly created droplet's status and features",
        predict={"status": 200, "note": "status new or active; no monitoring in features"},
    )
    if isinstance(r.body, dict):
        d = r.body["droplet"]
        print(f"    status={d['status']} features={d.get('features')}")


def sweep(probe: Probe) -> int:
    status, body = probe._send("GET", f"/droplets?tag_name={SYSTEM_TEST_TAG}&per_page=200", None)
    if status != 200 or not isinstance(body, dict):
        print(f"sweep: could not list droplets ({status})")
        return 0
    leaked = 0
    now = datetime.datetime.now(datetime.UTC)
    for d in body.get("droplets", []):
        if not d["name"].startswith(DROPLET_PREFIX):
            continue
        created = datetime.datetime.fromisoformat(d["created_at"])
        age = (now - created).total_seconds() / 60
        if age < SWEEP_MIN_AGE_MINUTES:
            continue
        print(f"  LEAKED droplet {d['id']} {d['name']} ({age:.0f}m old)")
        leaked += 1
        if probe.mutate:
            probe.call("DELETE", f"/droplets/{d['id']}", note="sweep: delete", record=False)
    return leaked


def main(argv=None) -> int:
    parser = base_arg_parser(__doc__)
    parser.add_argument(
        "--reference-droplet",
        required=False,
        help=(
            "id of an existing droplet whose agent is already reporting. Required for "
            "the read-only probes: a droplet created by this session has no history, "
            "which is a different question (probe 10)."
        ),
    )
    args = parser.parse_args(argv)
    with Probe(SESSION, mutate=args.mutate, dry_run=args.dry_run, audit=not args.sweep) as probe:
        if args.sweep:
            n = sweep(probe)
            print(f"\n{n} leftover(s)" + (" -- investigate" if n else ""))
            return 1 if n else 0
        if not args.reference_droplet and not args.dry_run:
            print("--reference-droplet is required (see --help)")
            return 2
        probe.reference_droplet_id = args.reference_droplet or "0"
        run(probe)
        print(f"\n{probe._seq} probes; {len(probe.contradictions)} contradicted a prediction:")
        for line in probe.contradictions:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
