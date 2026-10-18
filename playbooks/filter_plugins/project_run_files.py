"""Expose the shared run_file filter to independently invoked child playbooks."""
import sys
from pathlib import Path

# Local Ansible shell/command tasks may start in the deployment user's home,
# where the project's ansible.cfg is not discovered. Ansible always searches
# the child playbook's adjacent filter_plugins directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'filter_plugins'))
from run_files import FilterModule
