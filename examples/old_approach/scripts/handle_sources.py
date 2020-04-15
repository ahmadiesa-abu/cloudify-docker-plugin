#!/usr/bin/env python
import os
import yaml

from uuid import uuid1
from tempfile import mkdtemp

from cloudify import ctx
from cloudify.state import ctx_parameters as inputs
from cloudify.exceptions import NonRecoverableError


CLOUDIFY_CREATE_OPERATION = 'cloudify.interfaces.lifecycle.create'
CLOUDIFY_DELETE_OPERATION = 'cloudify.interfaces.lifecycle.delete'
CLOUDIFY_CONTEXT = "ctx"
CLOUDIFY_SCRIPT_PATH = "script_path"
CONTAINER_VOLUME = "container_volume"
EXECLUDE_INPUTS = (CLOUDIFY_CONTEXT, CLOUDIFY_SCRIPT_PATH, CONTAINER_VOLUME)

ANSIBLE_PRIVATE_KEY = 'ansible_ssh_private_key_file'
HOSTS_FILE_NAME = 'hosts'


def main():
    hosts_path = mkdtemp()
    hosts_file = os.path.join(hosts_path, HOSTS_FILE_NAME)
    operation_name = ctx.operation.name or ""
    ctx.logger.info(
        'Handling sources for operation {0}'.format(operation_name))
    inputs.pop(CLOUDIFY_CONTEXT)
    inputs.pop(CLOUDIFY_SCRIPT_PATH)
    if operation_name == CLOUDIFY_CREATE_OPERATION:

        private_key_val = inputs.get(ANSIBLE_PRIVATE_KEY, "")
        if private_key_val:
            try:
                is_file_path = os.path.exists(private_key_val)
            except TypeError:
                is_file_path = False
            if not is_file_path:
                private_key_file = os.path.join(hosts_path, str(uuid1()))
                with open(private_key_file, 'w') as outfile:
                    outfile.write(private_key_val)
                os.chmod(private_key_file, 0o600)
                inputs.update({ANSIBLE_PRIVATE_KEY: private_key_file})
        else:
            raise NonRecoverableError("No private key was provided")
        # could be updated
        private_key_val = inputs.get(ANSIBLE_PRIVATE_KEY, "")
        # inputs is of type proxy_tools.Proxy -> can't dump it
        hosts_dict = {
            "all": {
                "hosts": {
                    "instance": {}
                }
            }
        }
        for key in inputs:
            if key in EXECLUDE_INPUTS:
                continue
            elif key == ANSIBLE_PRIVATE_KEY:
                # replace docker mapping to container volume
                hosts_dict['all']['hosts']['instance'][key] = \
                    inputs.get(key).replace(hosts_path,
                                            inputs.get(CONTAINER_VOLUME))
            else:
                hosts_dict['all']['hosts']['instance'][key] = inputs.get(key)
        with open(hosts_file, 'w') as outfile:
            yaml.safe_dump(hosts_dict, outfile, default_flow_style=False)

        ctx.instance.runtime_properties['hosts_file'] = hosts_file
        ctx.instance.runtime_properties['private_key'] = private_key_val
        ctx.instance.update()
    elif operation_name == CLOUDIFY_DELETE_OPERATION:
        try:
            if os.path.exists(hosts_file):
                os.remove(hosts_file)
        except TypeError:
            ctx.logger.info("hosts file doesn't exist")


if __name__ == "__main__":
    main()
