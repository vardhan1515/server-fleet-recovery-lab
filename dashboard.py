"""Provision the lab dashboard and start a loopback-only Grafana instance."""
import json
import os

from drills import fetch, until, write_json

PANELS = (
    ("Host reachability", 'up{job="linux_lab"}', "short"),
    ("HTTP service health", "lab_service_up", "short"),
    ("Scratch filesystem usage", "lab_disk_used_percent", "percent"),
    ("Shared DNS reachability", "lab_dns_up", "short"),
    ("Firing alerts by routing mode", 'sum by (mode, incident) (ALERTS{alertstate="firing"})', "short"),
)


def dashboard():
    datasource = {"type": "prometheus", "uid": "lab-prometheus"}
    panels = []
    for i, (title, expression, unit) in enumerate(PANELS):
        panels.append({
            "id": i + 1, "title": title, "type": "timeseries",
            "gridPos": {"x": 12 * (i % 2), "y": 8 * (i // 2), "w": 12, "h": 8},
            "datasource": datasource,
            "targets": [{"expr": expression, "refId": "A", "datasource": datasource,
                         "legendFormat": "{{host}}" if i < 4 else "{{mode}} / {{incident}}"}],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0}, "overrides": []},
        })
    return {"uid": "fleet-incident-lab", "title": "Fleet Incident and Recovery Lab",
            "schemaVersion": 39, "version": 1, "refresh": "5s",
            "time": {"from": "now-15m", "to": "now"}, "panels": panels}


def start_grafana(lab, base, prometheus):
    state = lab.args.state / "grafana"
    provision = state / "provisioning"
    for part in ("data", "logs"):
        (state / part).mkdir(parents=True, exist_ok=True)
    for part in ("datasources", "dashboards", "json"):
        (provision / part).mkdir(parents=True, exist_ok=True)
    write_json(provision / "datasources" / "lab.yml", {"apiVersion": 1, "datasources": [
        {"name": "Lab Prometheus", "uid": "lab-prometheus", "type": "prometheus",
         "access": "proxy", "url": prometheus, "isDefault": True}
    ]})
    write_json(provision / "dashboards" / "lab.yml", {"apiVersion": 1, "providers": [
        {"name": "Lab", "type": "file", "options": {"path": str(provision / "json")}}
    ]})
    write_json(provision / "json" / "lab.json", dashboard())
    write_json(lab.args.output / "grafana-dashboard.json", dashboard())
    env = os.environ.copy()
    env.update(
        GF_PATHS_DATA=str(state / "data"), GF_PATHS_LOGS=str(state / "logs"),
        GF_PATHS_PROVISIONING=str(provision), GF_SERVER_HTTP_ADDR="127.0.0.1",
        GF_SERVER_HTTP_PORT=base.rsplit(":", 1)[1], GF_AUTH_ANONYMOUS_ENABLED="true",
        GF_AUTH_ANONYMOUS_ORG_ROLE="Viewer", GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION="true",
        GF_ANALYTICS_REPORTING_ENABLED="false", GF_ANALYTICS_CHECK_FOR_UPDATES="false",
        GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES="false",
    )
    executable = lab.args.grafana
    lab.spawn("grafana", [executable, "server", "--homepath", executable.parent.parent], env)
    until(lambda: fetch(base + "/api/dashboards/uid/fleet-incident-lab"), 60,
          "Grafana dashboard provisioning")
    health = fetch(base + "/api/datasources/uid/lab-prometheus/health")
    (lab.args.output / "grafana-datasource-health.json").write_bytes(health)
    if json.loads(health).get("status") != "OK":
        raise RuntimeError("Grafana Prometheus datasource health check failed")
    sample = fetch(base + "/api/datasources/proxy/uid/lab-prometheus/api/v1/query"
                   "?query=count%28lab_service_up%29")
    rows = json.loads(sample)["data"]["result"]
    if len(rows) != 1 or float(rows[0]["value"][1]) != 2:
        raise RuntimeError("Grafana did not query telemetry for both guests")
    (lab.args.output / "grafana-query-check.json").write_bytes(sample)
    print(f"Grafana dashboard: {base}/d/fleet-incident-lab", flush=True)
