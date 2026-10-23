#!/usr/bin/env python3
"""Create a disabled, unstarted metadata container with installed Kolla APIs.

Installed only into a temporary Kolla tree by prepare_metadata.py. The native
module's argument handling and Docker/systemd workers retain site overrides.
"""


def main():
    from ansible.module_utils.migration_kolla_api import generate_module
    from ansible.module_utils.kolla_docker_worker import DockerWorker
    from ansible.module_utils.migration_metadata_common import preparation

    module = generate_module()
    worker = None
    try:
        if module.params.get('container_engine') != 'docker':
            raise ValueError('Metadata preparation supports the Docker lab only')
        worker = DockerWorker(module)
        evidence = preparation(worker)
        module.exit_json(changed=worker.changed, preparation=evidence)
    except Exception as exc:
        module.fail_json(changed=bool(worker and worker.changed), msg=str(exc))


if __name__ == '__main__':
    main()
