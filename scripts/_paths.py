"""Source paths for grouped controller helpers.

Remote and guest agents are staged as flat sibling files. Their imports stay
flat on purpose; only a source checkout calls configure(). No installation or
working-directory assumption is required.
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_FILES = {
    "aux_ping.py": "probes/aux_ping.py",
    "cutover_preparation.py": "migration/cutover_preparation.py",
    "dataplane_capture.py": "diagnostics/dataplane_capture.py",
    "diagnostic_policy.py": "common/diagnostic_policy.py",
    "diagnostics_control.py": "diagnostics/diagnostics_control.py",
    "extended_validation.py": "validation/extended_validation.py",
    "guest_probe.py": "probes/guest_probe.py",
    "live_recovery.py": "validation/live_recovery.py",
    "migration_diagnostics.py": "diagnostics/migration_diagnostics.py",
    "migration_report.py": "reporting/migration_report.py",
    "network_semantics.py": "migration/network_semantics.py",
    "ns_control.py": "probes/ns_control.py",
    "ns_diagnostics.py": "diagnostics/ns_diagnostics.py",
    "ns_endpoint.py": "validation/ns_endpoint.py",
    "ns_evidence_summary.py": "reporting/ns_evidence_summary.py",
    "ns_icmp.py": "probes/ns_icmp.py",
    "ns_icmp_evidence.py": "diagnostics/ns_icmp_evidence.py",
    "ns_icmp_logging.py": "diagnostics/ns_icmp_logging.py",
    "ns_icmp_reconcile.py": "diagnostics/ns_icmp_reconcile.py",
    "ns_log_transfer.py": "diagnostics/ns_log_transfer.py",
    "ns_preparation_retry.py": "recovery/ns_preparation_retry.py",
    "ns_probe.py": "probes/ns_probe.py",
    "ns_underlay.py": "reset/ns_underlay.py",
    "openflow_prepare.py": "migration/openflow_prepare.py",
    "ovn_workload_evidence.py": "diagnostics/ovn_workload_evidence.py",
    "phase_schema.py": "common/phase_schema.py",
    "post_db_recovery.py": "recovery/post_db_recovery.py",
    "pre_stage_recovery.py": "recovery/pre_stage_recovery.py",
    "prepare_migration_images.py": "migration/prepare_migration_images.py",
    "provider_associations.py": "migration/provider_associations.py",
    "report_format.py": "reporting/report_format.py",
    "reset_demo.py": "reset/reset_demo.py",
    "reset_host_observers.py": "reset/reset_host_observers.py",
    "reset_local_probes.py": "reset/reset_local_probes.py",
    "reset_rabbitmq_health.py": "reset/reset_rabbitmq_health.py",
    "reset_rabbitmq_recovery.py": "reset/reset_rabbitmq_recovery.py",
    "reset_validation.py": "reset/reset_validation.py",
    "run_layout.py": "common/run_layout.py",
    "shared_external.py": "common/shared_external.py",
    "source_topology.py": "migration/source_topology.py",
    "tcp_guest_probe.py": "probes/tcp_guest_probe.py",
    "tcp_trigger.py": "probes/tcp_trigger.py",
    "tcp_validation.py": "validation/tcp_validation.py",
    "traffic_timeline.py": "reporting/traffic_timeline.py",
    "validation_placement.py": "validation/validation_placement.py",
    "validation_prerequisites.py": "validation/validation_prerequisites.py",
    "workload_validation.py": "validation/workload_validation.py",
}
PLAYBOOK_FILES = {
    "dataplane-capture-tasks.yml": "transport/dataplane-capture-tasks.yml",
    "diagnostics-checkpoint-tasks.yml": "tasks/diagnostics-checkpoint-tasks.yml",
    "diagnostics-control.yml": "transport/diagnostics-control.yml",
    "ns-control-tasks.yml": "transport/ns-control-tasks.yml",
    "ns-diagnostics-tasks.yml": "transport/ns-diagnostics-tasks.yml",
    "ns-source-profile-tasks.yml": "tasks/ns-source-profile-tasks.yml",
    "openflow-preparation-tasks.yml": "tasks/openflow-preparation-tasks.yml",
    "pair-d-trigger-tasks.yml": "transport/pair-d-trigger-tasks.yml",
    "reset-demo-preflight-tasks.yml": "tasks/reset-demo-preflight-tasks.yml",
    "reset-host-observers.yml": "transport/reset-host-observers.yml",
    "resume-bootstrap.yml": "recovery/resume-bootstrap.yml",
    "validation-snapshot-tasks.yml": "tasks/validation-snapshot-tasks.yml",
    "workload-ovn-evidence-tasks.yml": "transport/workload-ovn-evidence-tasks.yml",
}


def configure():
    """Expose the fixed, unique module groups to legacy flat agent imports."""
    for group in reversed(
        (
            "common",
            "migration",
            "validation",
            "probes",
            "diagnostics",
            "reporting",
            "reset",
            "recovery",
        )
    ):
        directory = str(PROJECT_ROOT / "scripts" / group)
        if directory not in sys.path:
            sys.path.insert(0, directory)


def script_path(name):
    """Resolve a known source helper, never a run artifact or remote filename."""
    return PROJECT_ROOT / "scripts" / SCRIPT_FILES[name]


def playbook_path(name):
    """Resolve a controller-invoked transport/helper independently of cwd."""
    return PROJECT_ROOT / "playbooks" / PLAYBOOK_FILES[name]
