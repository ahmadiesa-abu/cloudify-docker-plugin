########
# Copyright (c) 2014-2020 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import io
import os
import sys
import json
import yaml
import errno
import socket
import shutil
import tempfile
import threading
import subprocess

import docker

from uuid import uuid1
from fabric.contrib.files import exists
from fabric.api import settings, put, sudo
from functools import wraps

from cloudify import ctx as ctx_from_import

from cloudify.manager import get_rest_client

from cloudify.decorators import operation
from cloudify.exceptions import (NonRecoverableError,
                                 OperationRetry,
                                 HttpException)

from cloudify_common_sdk.resource_downloader import unzip_archive
from cloudify_common_sdk.resource_downloader import untar_archive
from cloudify_common_sdk.resource_downloader import get_shared_resource
from cloudify_common_sdk.resource_downloader import TAR_FILE_EXTENSTIONS

HOSTS_FILE_NAME = 'hosts'
CONTAINER_VOLUME = "container_volume"
PLAYBOOK_PATH = "playbook_path"
ANSIBLE_PRIVATE_KEY = 'ansible_ssh_private_key_file'
LOCAL_HOST_ADDRESSES = ("127.0.0.1", "localhost", "host.docker.internal")
WORKSPACE = 'workspace'
HOSTS = 'hosts'
BP_INCLUDES_PATH = '/opt/manager/resources/blueprints/' \
                   '{tenant}/{blueprint}/{relative_path}'


def get_lan_ip():

    def get_interface_ip(ifname):
        if os.name != "nt":
            import fcntl
            import struct
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return socket.inet_ntoa(fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack('256s', bytes(ifname[:15]))
                # Python 3: add 'utf-8' to bytes
            )[20:24])
        return "127.0.0.1"

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip.startswith("127.") and os.name != "nt":
            interfaces = ["eth0", "eth1", "eth2", "wlan0", "wlan1", "wifi0",
                "ath0","ath1","ppp0"]
            for ifname in interfaces:
                try:
                    ip = get_interface_ip(ifname)
                    break;
                except IOError:
                    pass
        return ip
    except socket.gaierror:
        return "127.0.0.1" # considering no IP is configured to begin with

def get_fabric_settings(server_ip, server_user, server_private_key):
    try:
        is_file_path = os.path.exists(server_private_key)
    except TypeError:
        is_file_path = False
    if not is_file_path:
        private_key_file = os.path.join(tempfile.mkdtemp(), str(uuid1()))
        with open(private_key_file, 'w') as outfile:
            outfile.write(server_private_key)
        os.chmod(private_key_file, 0o600)
        server_private_key = private_key_file
    return settings(
        connection_attempts=5,
        disable_known_hosts=True,
        warn_only=True,
        host_string=server_ip,
        key_filename=private_key_file,
        user=server_user)


def handle_docker_exception(func):
    @wraps(func)
    def f(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except docker.errors.APIError as ae:
            raise NonRecoverableError(str(ae))
        except docker.errors.DockerException as de:
            raise NonRecoverableError(str(de))
        except Exception as e:
            ctx = kwargs['ctx']
            ctx.logger.error("Generic exception {0}".format(str(e)))
            raise NonRecoverableError(str(e))
    return f


def with_docker(func):
    @wraps(func)
    def f(*args, **kwargs):
        ctx = kwargs['ctx']
        base_url = "tcp://{0}:{1}".format(
            ctx.node.properties['client_config']['docker_host'],
            ctx.node.properties['client_config']['docker_rest_port'])
        kwargs['docker_client'] = docker.Client(base_url=base_url, tls=False)
        return func(*args, **kwargs)
    return f


@handle_docker_exception
def follow_container_logs(ctx, docker_client, container, **kwargs):

    @handle_docker_exception
    def stop_follow_function(container_socket):
        container_socket.close()

    run_output = ""
    container_logs = docker_client.attach(container, stream=True)
    ctx.logger.info("Following container {0} logs".format(container))
    ctx.logger.info("Attach returned {0}".format(container_logs))
    # stop after 2 minutes at max
    timer = threading.Timer(120, stop_follow_function, args=[container_logs])
    timer.start()
    try:
        for chunk in container_logs:
            run_output += "{0}\n".format(chunk)
            ctx.logger.info("{0}".format(chunk))
    finally:
        timer.cancel()
    if not run_output:
        container_logs = docker_client.logs(container, stream=True)
        for chunk in container_logs:
            run_output += "{0}\n".format(chunk)
            ctx.logger.info("{0}".format(chunk))
    return run_output


def move_files(source, destination, permissions=None):
    for filename in os.listdir(source):
        if destination == os.path.join(source, filename):
            # moving files from parent to child case
            # so skip
            continue
        shutil.move(os.path.join(source, filename),
                    os.path.join(destination, filename))
        if permissions:
            os.chmod(os.path.join(destination, filename), permissions)


@operation
def prepare_container_files(ctx, **kwargs):

    docker_ip = \
        ctx.node.properties.get('resource_config',{}).get('docker_machine',
            {}).get('docker_ip',"")
    docker_user = \
        ctx.node.properties.get('resource_config',{}).get('docker_machine',
            {}).get('docker_user',"")
    docker_key = \
        ctx.node.properties.get('resource_config',{}).get('docker_machine',
            {}).get('docker_key',"")
    source = \
        ctx.node.properties.get('resource_config',{}).get('source',"")
    destination = \
        ctx.node.properties.get('resource_config',{}).get('destination',"")
    extra_files = \
        ctx.node.properties.get('resource_config',{}).get('extra_files',{})
    ansible_sources = \
        ctx.node.properties.get('resource_config',{}).get('ansible_sources',{})
    terraform_sources = \
        ctx.node.properties.get('resource_config',{}).get('terraform_sources',
                                                          {})
    # check source to handle various cases [zip,tar,git]
    source_tmp_path = get_shared_resource(source)
    # check if we actually downloaded something or not
    if source_tmp_path == source:
        # didn't download anything so check the provided path
        # if file and absolute path or not
        if not os.path.isabs(source_tmp_path):
            # bundled and need to be downloaded from blurprint
            source_tmp_path = ctx.download_resource(source_tmp_path)
        if os.path.isfile(source_tmp_path):
            file_name = source_tmp_path.rsplit('/', 1)[1]
            file_type = file_name.rsplit('.', 1)[1]
            # check type
            if file_type == 'zip':
                source_tmp_path = unzip_archive(source_tmp_path)
            elif file_type in TAR_FILE_EXTENSTIONS:
                source_tmp_path = untar_archive(source_tmp_path)

    # Reaching this point we should have got the files into source_tmp_path
    if not destination:
        destination = tempfile.mkdtemp()
    move_files(source_tmp_path, destination)
    shutil.rmtree(source_tmp_path)

    # copy extra files to destination
    for file in extra_files:
        try:
            is_file_path = os.path.exists(file)
            if is_file_path:
                shutil.copy(file, destination)
        except TypeError:
            ctx.logger.error("file {0} can't be copied".format(file))

    # handle ansible_sources -Special Case-:
    if ansible_sources:
        hosts_file = os.path.join(destination, HOSTS_FILE_NAME)
        # handle the private key logic
        private_key_val = ansible_sources.get(ANSIBLE_PRIVATE_KEY, "")
        if private_key_val:
            try:
                is_file_path = os.path.exists(private_key_val)
            except TypeError:
                is_file_path = False
            if not is_file_path:
                private_key_file = os.path.join(destination, str(uuid1()))
                with open(private_key_file, 'w') as outfile:
                    outfile.write(private_key_val)
                os.chmod(private_key_file, 0o600)
                ansible_sources.update({ANSIBLE_PRIVATE_KEY: private_key_file})
        else:
            raise NonRecoverableError(
                "Check Ansible Sources, No private key was provided")
        # check if playbook_path was provided or not
        playbook_path = ansible_sources.get(PLAYBOOK_PATH, "")
        if not playbook_path:
            raise NonRecoverableError(
                "Check Ansible Sources, No playbook path was provided")
        hosts_dict = {
            "all":{
                "hosts":{
                    "instance":{}
                }
            }
        }
        for key in ansible_sources:
            if key in (CONTAINER_VOLUME, PLAYBOOK_PATH):
                continue
            elif key==ANSIBLE_PRIVATE_KEY:
                # replace docker mapping to container volume
                hosts_dict['all']['hosts']['instance'][key] = \
                    ansible_sources.get(key).replace(destination,
                        ansible_sources.get(CONTAINER_VOLUME))
            else:
                hosts_dict['all']['hosts']['instance'][key] = \
                    ansible_sources.get(key)
        with open(hosts_file, 'w') as outfile:
            yaml.safe_dump(hosts_dict, outfile, default_flow_style=False)
        ctx.instance.runtime_properties['ansible_container_command_arg'] = \
            "ansible-playbook -i hosts {0}".format(playbook_path)

    # handle terraform_sources -Special Case-:
    if terraform_sources:
        container_volume = terraform_sources.get(CONTAINER_VOLUME, "")
        # handle files
        storage_dir = terraform_sources.get("storage_dir", "")
        if not storage_dir:
            storage_dir = os.path.join(destination, str(uuid1()))
        else:
            storage_dir = os.path.join(destination, storage_dir)
        os.mkdir(storage_dir)
        # move the downloaded files from source to storage_dir
        move_files(destination, storage_dir)
        # store the runtime property relative to container rather than docker
        storage_dir_prop = storage_dir.replace(destination, container_volume)
        ctx.instance.runtime_properties['storage_dir'] = storage_dir_prop

        # handle plugins
        plugins_dir = terraform_sources.get("plugins_dir", "")
        if not plugins_dir:
            plugins_dir = os.path.join(destination, str(uuid1()))
        else:
            plugins_dir = os.path.join(destination, plugins_dir)
        plugins = terraform_sources.get("plugins", {})
        os.mkdir(plugins_dir)
        for plugin in plugins:
            downloaded_plugin_path = get_shared_resource(plugin)
            if downloaded_plugin_path == plugin:
                # it means we didn't download anything/ extracted
                raise NonRecoverableError(
                    "Check Plugin {0} URL".format(plugin))
            else:
                move_files(downloaded_plugin_path, plugins_dir, 0o775)
        os.chmod(plugins_dir, 0o775)
        # store the runtime property relative to container rather than docker
        plugins_dir = plugins_dir.replace(destination, container_volume)
        ctx.instance.runtime_properties['plugins_dir'] = plugins_dir

        # handle variables
        terraform_variables = terraform_sources.get("variables", {})
        if terraform_variables:
            variables_file = os.path.join(storage_dir, 'vars.json')
            with open(variables_file, 'w') as outfile:
                json.dump(terraform_variables, outfile)
            # store the runtime property relative to container
            # rather than docker
            variables_file = \
                variables_file.replace(destination, container_volume)
            ctx.instance.runtime_properties['variables_file'] = variables_file

        # handle backend
        terraform_backend = terraform_sources.get("backend", {})
        if terraform_backend:
            if not terraform_backend.get("name", ""):
                raise NonRecoverableError(
                    "Check backend {0} name value".format(terraform_backend))
            backend_str = """
                terraform {
                    backend "{backend_name}" {
                        {backend_options}
                    }
                }
            """
            backend_options = ""
            for option_name, option_value in \
                terraform_backend.get("options", {}).items():
                if isinstance(option_value, basestring):
                    backend_options += "{0} = \"{1}\"".format(option_name,
                                                              option_value)
                else:
                    backend_options += "{0} = {1}".format(option_name,
                                                          option_value)
            backend_str.format(
                backend_name=terraform_backend.get("name"),
                backend_options=backend_options)
            backend_file = os.path.join(storage_dir, '{0}.tf'.format(
                terraform_backend.get("name")))
            with open(backend_file, 'w') as outfile:
                outfile.write(backend_str)
            # store the runtime property relative to container
            # rather than docker
            backend_file = \
                backend_file.replace(destination, container_volume)
            ctx.instance.runtime_properties['backend_file'] = backend_file

        # handle terraform scripts inside shell script
        terraform_script_file = os.path.join(storage_dir, '{0}.sh'.format(
            str(uuid1())))
        terraform_script="""#!/bin/bash -e
terraform init -no-color -plugin-dir={plugins_dir} {storage_dir}
terraform plan -no-color {vars_file} {storage_dir}
terraform apply -no-color -auto-approve {vars_file} {storage_dir}
terraform refresh -no-color {vars_file}
terraform state pull
        """.format(plugins_dir=plugins_dir,
            storage_dir=storage_dir_prop,
            vars_file="" if not terraform_variables
                         else " -var-file {0}".format(variables_file))
        ctx.logger.info("terraform_script_file content {0}".format(
            terraform_script))
        with open(terraform_script_file, 'w') as outfile:
            outfile.write(terraform_script)
        # store the runtime property relative to container
        # rather than docker
        terraform_script_file = \
            terraform_script_file.replace(destination, container_volume)
        ctx.instance.runtime_properties['terraform_script_file'] = \
            terraform_script_file
        ctx.instance.runtime_properties['terraform_container_command_arg'] = \
            "bash {0}".format(terraform_script_file)

    # Reaching this point means we now have everything in this destination
    ctx.instance.runtime_properties['destination'] = destination
    ctx.instance.runtime_properties['docker_host'] = docker_ip
    # copy these files to docker machine if needed at that destination
    if docker_ip not in (LOCAL_HOST_ADDRESSES, get_lan_ip()):
        with get_fabric_settings(docker_ip,
                                 docker_user,
                                 docker_key):
            destination_parent = destination.rsplit('/', 1)[0]
            if destination_parent != '/tmp':
                sudo('mkdir -p {0}'.format(destination_parent))
                sudo("chown -R {0}:{0} {1}".format(docker_user,
                                                   destination_parent))
            put(destination, destination_parent, mirror_local_mode=True)


@operation
def remove_container_files(ctx, **kwargs):

    docker_ip = \
        ctx.node.properties.get('resource_config',{}).get('docker_ip',"")
    docker_user = \
        ctx.node.properties.get('resource_config',{}).get('docker_user',"")
    docker_key = \
        ctx.node.properties.get('resource_config',{}).get('docker_key',"")

    destination = ctx.instance.runtime_properties.get('destination',"")
    if not destination:
        ctx.logger.error("destination was not assigned due to error")
        return
    ctx.logger.info("removing file from destination {0}".format(destination))
    shutil.rmtree(destination)
    ctx.instance.runtime_properties.pop('destination', None)
    if docker_ip not in (LOCAL_HOST_ADDRESSES, get_lan_ip()):
        with get_fabric_settings(docker_ip, docker_user, docker_key):
            sudo("rm -rf {0}".format(destination))


@operation
@handle_docker_exception
@with_docker
def list_images(ctx, docker_client, **kwargs):
    ctx.instance.runtime_properties['images'] = docker_client.images(all=True)


@operation
@handle_docker_exception
@with_docker
def list_host_details(ctx, docker_client, **kwargs):
    ctx.instance.runtime_properties['host_details'] = docker_client.info()


@operation
@handle_docker_exception
@with_docker
def list_containers(ctx, docker_client, **kwargs):
    ctx.instance.runtime_properties['contianers'] = \
        docker_client.containers(all=True, trunc=True)


@operation
@handle_docker_exception
@with_docker
def build_image(ctx, docker_client, **kwargs):
    image_content = \
        ctx.node.properties.get('resource_config',{}).get('image_content',"")
    tag = \
        ctx.node.properties.get('resource_config',{}).get('tag',"")
    if image_content:
        ctx.logger.info("Building image with tag {0}".format(tag))
        # replace the new line str with new line char
        image_content = image_content.replace("\\n",'\n')
        ctx.logger.info("Image Dockerfile {0}".format(image_content))
        build_output = ""
        img_data = io.BytesIO(image_content.encode('ascii'))
        for chunk in docker_client.build(fileobj=img_data, tag=tag):
            build_output += "{0}\n".format(chunk)
        ctx.instance.runtime_properties['build_result'] = build_output
        ctx.logger.info("Build Output {0}".format(build_output))
        if 'errorDetail' in build_output:
            raise NonRecoverableError("Build Failed check build-result")
        ctx.instance.runtime_properties['image'] =  \
            docker_client.images(name=tag)


@operation
@handle_docker_exception
@with_docker
def remove_image(ctx, docker_client, **kwargs):
    tag = \
        ctx.node.properties.get('resource_config',{}).get('tag',"")
    build_res = ctx.instance.runtime_properties.pop('build_result',"")
    if tag:
        if not build_res or 'errorDetail' in build_res:
            ctx.logger.info("build contained errors , nothing to do ")
            return
        ctx.logger.info("Removing image with tag {0}".format(tag))
        remove_res = docker_client.remove_image(tag, force=True)
        ctx.logger.info("Remove result {0}".format(remove_res))


@operation
@handle_docker_exception
@with_docker
def create_conatiner(ctx, docker_client, **kwargs):
    image_tag = \
        ctx.node.properties.get('resource_config',{}).get('image_tag',"")
    container_args = \
        ctx.node.properties.get('resource_config',{}).get('container_args',{})
    if image_tag:
        ctx.logger.info(
            "Running container from image tag {0}".format(image_tag))
        run_output = ""
        host_config = container_args.get("host_config", {})

        # handle volume mapping
        # map each entry to it's volume based on index
        volumes = container_args.get('volumes', None)
        if volumes:
            # logic was added to handle mapping to create_container
            paths_on_host = container_args.pop('volumes_mapping', None)
            binds_list = []
            if paths_on_host:
                for path, volume in zip(paths_on_host, volumes):
                    binds_list.append('{0}:{1}'.format(path, volume))
                host_config.update({"binds":binds_list})
        ctx.logger.info("host_config : {0}".format(host_config))
        # lots but these to handle *args in create_host_config
        host_config = docker_client.create_host_config(
            binds=None if not host_config.get("binds", None)
                       else host_config.get("binds", None),
            port_bindings=None if not host_config.get("port_bindings", None)
                       else host_config.get("port_bindings", None),
            lxc_conf=None if not host_config.get("lxc_conf", None)
                       else host_config.get("lxc_conf", None),
            publish_all_ports=False,
            links=None if not host_config.get("links", None)
                       else host_config.get("links", None),
            privileged=False,
            dns=None if not host_config.get("dns", None)
                     else host_config.get("dns", None),
            dns_search=None if not host_config.get("dns_search", None)
                            else host_config.get("dns_search", None),
            volumes_from=None if not host_config.get("volumes_from", None)
                              else host_config.get("volumes_from", None),
            network_mode=None if not host_config.get("network_mode", None)
                              else host_config.get("network_mode", None),
            restart_policy=None if not host_config.get("restart_policy", None)
                                else host_config.get("restart_policy", None),
            cap_add=None if not host_config.get("cap_add", None)
                         else host_config.get("cap_add", None),
            cap_drop=None if not host_config.get("cap_drop", None)
                          else host_config.get("cap_drop", None),
            devices=None if not host_config.get("devices", None)
                         else host_config.get("devices", None),
            extra_hosts=None if not host_config.get("extra_hosts", None)
                             else host_config.get("extra_hosts", None),
            read_only=None if not host_config.get("read_only", None)
                           else host_config.get("read_only", None),
            pid_mode=None if not host_config.get("pid_mode", None)
                          else host_config.get("pid_mode", None),
            ipc_mode=None if not host_config.get("ipc_mode", None)
                          else host_config.get("ipc_mode", None),
            security_opt=None if not host_config.get("security_opt", None)
                              else host_config.get("security_opt", None),
            ulimits=None if not host_config.get("ulimits", None)
                         else host_config.get("ulimits", None),
            log_config=None if not host_config.get("log_config", None)
                            else host_config.get("log_config", None),
            mem_limit=None if not host_config.get("mem_limit", None)
                           else host_config.get("mem_limit", None),
            memswap_limit=None if not host_config.get("memswap_limit", None)
                               else host_config.get("memswap_limit", None),
            mem_reservation=None if not host_config.get("mem_reservation", None)
                                 else host_config.get("mem_reservation", None),
            kernel_memory=None if not host_config.get("kernel_memory", None)
                               else host_config.get("kernel_memory", None),
            mem_swappiness=None if not host_config.get("mem_swappiness", None)
                                else host_config.get("mem_swappiness", None),
            cgroup_parent=None if not host_config.get("cgroup_parent", None)
                               else host_config.get("cgroup_parent", None),
            group_add=None if not host_config.get("group_add", None)
                           else host_config.get("group_add", None),
            cpu_quota=None if not host_config.get("cpu_quota", None)
                           else host_config.get("cpu_quota", None),
            cpu_period=None if not host_config.get("cpu_period", None)
                            else host_config.get("cpu_period", None),
            blkio_weight=None if not host_config.get("blkio_weight", None)
                              else host_config.get("blkio_weight", None),
            blkio_weight_device=\
                None if not host_config.get("blkio_weight_device", None)
                     else host_config.get("blkio_weight_device", None),
            device_read_bps=None if not host_config.get("device_read_bps", None)
                                 else host_config.get("device_read_bps", None),
            device_write_bps=\
                None if not host_config.get("device_write_bps", None)
                     else host_config.get("device_write_bps", None),
            device_read_iops=\
                None if not host_config.get("device_read_iops", None)
                     else host_config.get("device_read_iops", None),
            device_write_iops=\
                None if not host_config.get("device_write_iops", None)
                     else host_config.get("device_write_iops", None),
            oom_kill_disable=False,
            shm_size=None if not host_config.get("shm_size", None)
                          else host_config.get("shm_size", None),
            sysctls=None if not host_config.get("sysctls", None)
                         else host_config.get("sysctls", None),
            # version=None if not host_config.get("version", None)
            #              else host_config.get("version", None),
            tmpfs=None if not host_config.get("tmpfs", None)
                       else host_config.get("tmpfs", None),
            oom_score_adj=None if not host_config.get("oom_score_adj", None)
                               else host_config.get("oom_score_adj", None),
            dns_opt=None if not host_config.get("dns_opt", None)
                         else host_config.get("dns_opt", None),
            cpu_shares=None if not host_config.get("cpu_shares", None)
                            else host_config.get("cpu_shares", None),
            cpuset_cpus=None if not host_config.get("cpuset_cpus", None)
                             else host_config.get("cpuset_cpus", None),
            userns_mode=None if not host_config.get("userns_mode", None)
                             else host_config.get("userns_mode", None),
            pids_limit=None if not host_config.get("pids_limit", None)
                            else host_config.get("pids_limit", None))

        ctx.instance.runtime_properties['host_config'] = host_config
        container_args['host_config'] = host_config

        container = docker_client.create_container(image=image_tag,
                                                   **container_args)
        ctx.logger.info("container was created : {0}".format(container))
        ctx.instance.runtime_properties['container'] = container
        # using the same docker_client connection for start that will actually
        # create the container
        if not container_args.get("command", ""):
            ctx.logger.info("no command sent to container, nothing to do")
            return
        ctx.logger.info(
            "Running this command on container : {0} ".format(
                container_args.get("command", "")))
        docker_client.start(container)
        container_logs = follow_container_logs(ctx, docker_client, container)
        ctx.logger.info("container logs : {0} ".format(container_logs))
        ctx.instance.runtime_properties['run_result'] = container_logs


@operation
@handle_docker_exception
@with_docker
def start_conatiner(ctx, docker_client, **kwargs):
    container_args = \
        ctx.node.properties.get('resource_config',{}).get('container_args',{})
    container = ctx.instance.runtime_properties.get('container',"")
    if not container:
        ctx.logger.info("container was not create successfully, nothing to do")
        return
    if not container_args.get("command", ""):
        ctx.logger.info("no command sent to container, nothing to do")
        return
    ctx.logger.info(
        "Running this command on container : {0} ".format(
            container_args.get("command", "")))
    docker_client.start(container)
    container_logs = follow_container_logs(ctx, docker_client, container)
    ctx.logger.info("container logs : {0} ".format(container_logs))
    ctx.instance.runtime_properties['run_result'] = container_logs


@operation
@handle_docker_exception
@with_docker
def stop_container(ctx, docker_client, stop_command, **kwargs):

    def check_if_applicable_command(command):
        EXCEPTION_LIST = ('terraform', 'ansible-playbook', 'ansible')
        # check if command given the platform ,
        # TODO : make it more dynamic
        # at least : bash , python , and basic unix commands ...
        # adding exceptions like terraform, ansible_playbook
        # if they are not installed on the host
        # can be extended based on needs
        if command in EXCEPTION_LIST:
            return True
        rc = subprocess.call(['which', command])
        if rc == 0:
            return True
        else:
            return False

    def handle_container_timed_out(ctx, docker_client, container_args,
        stop_command):

        # check the original command in the properties
        command = container_args.get("command", "")
        if not command:
            ctx.logger.info("no command sent to container, nothing to do")
            return
        # assuming the container was passed : {script_executor} {script} [ARGS]
        if len(command.split(' ',1))>=2:
            script_executor = command.split(' ',1)[0]
            if not check_if_applicable_command(script_executor):
                ctx.logger.info(
                    "can't run this command {0}".format(script_executor))
                return
            # here we assume the command is OK , and we have arguments to it
            script = command.split(' ',1)[1].split()[0]
            ctx.logger.info("script to override {0}".format(script))
            # Handle the attached volume to override
            # the script with stop_command
            volumes = container_args.get("volumes", "")
            volumes_mapping = container_args.get("volumes_mapping", "")
            # look for the script in the mapped volumes
            mapping_to_use = ""
            for volume, mapping in zip(volumes, volumes_mapping):
                ctx.logger.info(
                    "check if script {0} contain volume {1}".format(script,
                        volume))
                if volume in script:
                    ctx.logger.info("replacing {0} with {1}".format(volume,
                        mapping))
                    script = script.replace(volume, mapping)
                    ctx.logger.info("script to modify is {0}".format(script))
                    mapping_to_use = mapping
                    break

            if not mapping_to_use:
                ctx.logger.info("volume mapping is not correct")
                return

            # if we are here , then we found the script
            # in one of the mapped volumes
            ctx.logger.info("overriding script {0} content to {1}".format(
                script, stop_command))
            with open(script, 'w') as outfile:
                outfile.write(stop_command)

            # we will get the docker_host conf from mapped
            # container_files node through relationships
            relationships = list(ctx.instance.relationships)
            for rel in relationships:
                node = rel.target.node
                if node.type == 'cloudify.nodes.docker.container_files':
                    docker_ip = \
                        node.properties.get('resource_config',
                            {}).get('docker_ip',"")
                    docker_user = \
                        node.properties.get('resource_config',
                            {}).get('docker_user',"")
                    docker_key = \
                        node.properties.get('resource_config',
                            {}).get('docker_key',"")
                    break
            if not docker_ip:
                ctx.logger.info(
                    "can't find docker_ip in container_files " + \
                    "node through relationships")
                return
            if docker_ip not in (LOCAL_HOST_ADDRESSES, get_lan_ip()):
                with get_fabric_settings(docker_ip, docker_user,
                    docker_key):
                    script_parent = script.rsplit('/', 1)[0]
                    put(script, script, mirror_local_mode=True)
            # now we can restart the container , and it will
            # run with the overriden script that contain the
            # stop_command
            docker_client.restart(container)
            container_logs = follow_container_logs(ctx, docker_client,
                container)
            ctx.logger.info("container logs : {0} ".format(container_logs))
        else:
                ctx.logger.info("""can't send this command {0} to container,
since it is unreachable""".format(stop_command))
                return

    container = ctx.instance.runtime_properties.get('container',"")
    image_tag = \
        ctx.node.properties.get('resource_config',{}).get('image_tag',"")
    container_args = \
        ctx.node.properties.get('resource_config',{}).get('container_args',{})
    if not stop_command:
        ctx.logger.info("no stop command, nothing to do")
        return

    script_executor = stop_command.split(' ',1)[0]
    if not check_if_applicable_command(script_executor):
        ctx.logger.info(
            "can't run this command {0}".format(script_executor))
        return

    if container:
        ctx.logger.info(
            "Stop Contianer {0} from tag {1} with command {2}".format(
                container, image_tag, stop_command))
        # attach to container socket and send the stop_command
        socket = docker_client.attach_socket(container,
                                                params={
                                                    'stdin': 1,
                                                    "stdout": 1,
                                                    'stream': 1,
                                                    "logs": 1
                                                })
        try:
            socket.settimeout(20) # timeout for 20 seconds
            socket.send(stop_command)
            buffer = ""
            while True:
                data = socket.recv(4096)
                if not data:
                    break
                buffer += data
            ctx.logger.info("Stop command result {0}".format(buffer))
        except docker.errors.APIError as ae:
            ctx.logger.error("APIError {1}".format(str(ae)))
        except Exception as e:
            message = e.message if hasattr(e, 'message') else e
            # response = e.response if hasattr(e, 'response') else e
            # explanation = e.explanation if hasattr(e, 'explanation') else e
            # errno = e.errno if hasattr(e, 'errno') else e
            ctx.logger.error("exception : {0}".format(message))
            # if timeout happened that means the container exited,
            # and if want to do something for the container,
            # or handle any special case if we want that
            if message == "timed out":
                # Special Handling for terraform -to call cleanup for example-
                # we can switch the command with stop_command and restart
                handle_container_timed_out(ctx, docker_client, container_args,
                    stop_command)

        socket.close()
        docker_client.stop(container)
        docker_client.wait(container)


@operation
@handle_docker_exception
@with_docker
def remove_container(ctx, docker_client, **kwargs):
    container = ctx.instance.runtime_properties.get('container',"")
    image_tag = \
        ctx.node.properties.get('resource_config',{}).get('image_tag',"")
    if container:
        ctx.logger.info(
            "remove Contianer {0} from tag {1}".format(container,
                                                       image_tag))
        remove_res = docker_client.remove_container(container)
        ctx.instance.runtime_properties.pop('container')
        ctx.logger.info("Remove result {0}".format(remove_res))


@operation
def set_playbook_config(ctx, **kwargs):
    """
    Set all playbook node instance configuration as runtime properties
    :param _ctx: Cloudify node instance which is instance of CloudifyContext
    :param config: Playbook node configurations
    """
    def _get_secure_values(data, sensitive_keys, parent_hide=False):
        """
        ::param data : dict to check againt sensitive_keys
        ::param sensitive_keys : a list of keys we want to hide the values for
        ::param parent_hide : boolean flag to pass if the parent key is
                                in sensitive_keys
        """
        for key in data:
            # check if key or its parent {dict value} in sensitive_keys
            hide = parent_hide or (key in sensitive_keys)
            value = data[key]
            # handle dict value incase sensitive_keys was inside another key
            if isinstance(value, dict):
                # call _get_secure_value function recusivly
                # to handle the dict value
                inner_dict = _get_secure_values(value, sensitive_keys, hide)
                data[key] = inner_dict
            else:
                data[key] = '*'*len(value) if hide else value
        return data
    if kwargs and isinstance(kwargs, dict):
        kwargs = _get_secure_values(kwargs, kwargs.get("sensitive_keys", {}))
        for key, value in kwargs.items():
            ctx.instance.runtime_properties[key] = value
    ctx.instance.update()

@operation
def create_ansible_playbook(ctx, **kwargs):

    def handle_file_path(file_path, additional_playbook_files, _ctx):
        """Get the path to a file.

        I do this for two reasons:
          1. The Download Resource only downloads an individual file.
          Ansible Playbooks are often many files.
          2. I have not figured out how to pass a file as an in
          memory object to the PlaybookExecutor class.

        :param file_path: The `site_yaml_path` from `run`.
        :param additional_playbook_files: additional files
          adjacent to the playbook path.
        :param _ctx: The Cloudify Context.
        :return: The absolute path on the manager to the file.
        """

        def _get_deployment_blueprint(deployment_id):
            new_blueprint = ""
            try:
                # get the latest deployment update to get the new blueprint id
                client = get_rest_client()
                dep_upd = \
                    client.deployment_updates.list(deployment_id=deployment_id,
                                                   sort='created_at')[-1]
                new_blueprint = \
                    client.deployment_updates.get(dep_upd.id)["new_blueprint_id"]
            except KeyError:
                raise NonRecoverableError(
                    "can't get blueprint for deployment {0}".format(deployment_id))
            return new_blueprint

        def download_nested_file_to_new_nested_temp_file(file_path, new_root, _ctx):
            """ Download file to a similar folder system with a new root directory.

            :param file_path: the resource path for download resource source.
            :param new_root: Like a temporary directory
            :param _ctx:
            :return:
            """

            dirname, file_name = os.path.split(file_path)
            # Create the new directory path including the new root.
            new_dir = os.path.join(new_root, dirname)
            new_full_path = os.path.join(new_dir, file_name)
            try:
                os.makedirs(new_dir)
            except OSError as e:
                if e.errno == errno.EEXIST and os.path.isdir(new_dir):
                    pass
                else:
                    raise
            return _ctx.download_resource(file_path, new_full_path)

        if not isinstance(file_path, basestring):
            raise NonRecoverableError(
                'The variable file_path {0} is a {1},'
                'expected a string.'.format(file_path, type(file_path)))
        if not getattr(_ctx, '_local', False):
            if additional_playbook_files:
                # This section is intended to handle scenario where we want
                # to download the resource instead of use absolute path.
                # Perhaps this should replace the old way entirely.
                # For now, the important thing here is that we are
                # enabling downloading the playbook to a remote host.
                playbook_file_dir = tempfile.mkdtemp()
                new_file_path = download_nested_file_to_new_nested_temp_file(
                    file_path,
                    playbook_file_dir,
                    _ctx
                )
                for additional_file in additional_playbook_files:
                    download_nested_file_to_new_nested_temp_file(
                        additional_file,
                        playbook_file_dir,
                        _ctx
                    )
                return new_file_path
            else:
                # handle update deployment different blueprint playbook name
                deployment_blueprint = _ctx.blueprint.id
                if _ctx.workflow_id == 'update':
                    deployment_blueprint = \
                        _get_deployment_blueprint(_ctx.deployment.id)
                file_path = \
                    BP_INCLUDES_PATH.format(
                        tenant=_ctx.tenant_name,
                        blueprint=deployment_blueprint,
                        relative_path=file_path)
        if os.path.exists(file_path):
            return file_path
        raise NonRecoverableError(
            'File path {0} does not exist.'.format(file_path))

    def handle_site_yaml(site_yaml_path, additional_playbook_files, _ctx):
        """ Create an absolute local path to the site.yaml.

        :param site_yaml_path: Relative to the blueprint.
        :param additional_playbook_files: additional playbook files relative to
          the playbook.
        :param _ctx: The Cloudify context.
        :return: The final absolute path on the system to the site.yaml.
        """

        site_yaml_real_path = os.path.abspath(
            handle_file_path(site_yaml_path, additional_playbook_files, _ctx))
        site_yaml_real_dir = os.path.dirname(site_yaml_real_path)
        site_yaml_real_name = os.path.basename(site_yaml_real_path)
        site_yaml_new_dir = os.path.join(
            _ctx.instance.runtime_properties[WORKSPACE], 'playbook')
        shutil.copytree(site_yaml_real_dir, site_yaml_new_dir)
        site_yaml_final_path = os.path.join(site_yaml_new_dir, site_yaml_real_name)
        return site_yaml_final_path

    def get_inventory_file(filepath, _ctx, new_inventory_path):
        """
        This method will get the location for inventory file.
        The file location could be locally with relative to the blueprint
        resources or it could be remotely on the remote machine
        :return:
        :param filepath: File path to do check for
        :param _ctx: The Cloudify context.
        :param new_inventory_path: New path which holds the file inventory path
        when "filepath" is a local resource
        :return: File location for inventory file
        """
        if os.path.isfile(filepath):
            # The file already exists on the system, then return the file url
            return filepath
        else:
            # Check to see if the file does not exit, then try to lookup the
            # file from the Cloudify blueprint resources
            try:
                _ctx.download_resource(filepath, new_inventory_path)
            except HttpException:
                _ctx.logger.error(
                    'Error when trying to download {0}'.format(filepath))
                return None
            return new_inventory_path

    def handle_source_from_string(filepath, _ctx, new_inventory_path):
        inventory_file = get_inventory_file(filepath, _ctx, new_inventory_path)
        if inventory_file:
            return inventory_file
        else:
            with open(new_inventory_path, 'w') as outfile:
                _ctx.logger.info(
                    'Writing this data to temp file: {0}'.format(
                        new_inventory_path))
                outfile.write(filepath)
        return new_inventory_path

    def handle_key_data(_data, workspace_dir):
        """Take Key Data from ansible_ssh_private_key_file and
        replace with a temp file.

        :param _data: The hosts dict (from YAML).
        :param workspace_dir: The temp dir where we are putting everything.
        :return: The hosts dict with a path to a temp file.
        """

        def recurse_dictionary(existing_dict,
            key='ansible_ssh_private_key_file'):
            if key not in existing_dict:
                for k, v in existing_dict.items():
                    if isinstance(v, dict):
                        existing_dict[k] = recurse_dictionary(v)
            elif key in existing_dict:
                # If is_file_path is True, this has already been done.
                try:
                    is_file_path = os.path.exists(existing_dict[key])
                except TypeError:
                    is_file_path = False
                if not is_file_path:
                    private_key_file = \
                        os.path.join(workspace_dir, str(uuid1()))
                    with open(private_key_file, 'w') as outfile:
                        outfile.write(existing_dict[key])
                    os.chmod(private_key_file, 0o600)
                    existing_dict[key] = private_key_file
            return existing_dict
        return recurse_dictionary(_data)

    def handle_sources(data, site_yaml_abspath, _ctx):
        """Allow users to provide a path to a hosts file
        or to generate hosts dynamically,
        which is more comfortable for Cloudify users.

        :param data: Either a dict (from YAML)
            or a path to a conventional Ansible file.
        :param site_yaml_abspath: This is the path to the site yaml folder.
        :param _ctx: The Cloudify context.
        :return: The final path of the hosts file that
            was either provided or generated.
        """

        hosts_abspath = os.path.join(os.path.dirname(site_yaml_abspath), HOSTS)
        if isinstance(data, dict):
            data = handle_key_data(
                data, _ctx.instance.runtime_properties[WORKSPACE])
            if os.path.exists(hosts_abspath):
                _ctx.logger.error(
                    'Hosts data was provided but {0} already exists. '
                    'Overwriting existing file.'.format(hosts_abspath))
            with open(hosts_abspath, 'w') as outfile:
                yaml.safe_dump(data, outfile, default_flow_style=False)
        elif isinstance(data, basestring):
            hosts_abspath = handle_source_from_string(data, _ctx, hosts_abspath)
        return hosts_abspath

    def prepare_options_config(options_config, run_data, destination):
        options_list = []
        if 'extra_vars' not in options_config:
            options_config['extra_vars'] = {}
        options_config['extra_vars'].update(run_data)
        for key, value in options_config.items():
            if key == 'extra_vars':
                f = tempfile.NamedTemporaryFile(delete=False, dir=destination)
                with open(f.name, 'w') as outfile:
                    json.dump(value, outfile)
                value = '@{filepath}'.format(filepath=f.name)
            elif key == 'verbosity':
                self.logger.error('No such option verbosity')
                del key
                continue
            key = key.replace("_", "-")
            if isinstance(value, basestring):
                value = value.encode('utf-8')
            elif isinstance(value, dict):
                value = json.dumps(value)
            elif isinstance(value, list) and key not in LIST_TYPES:
                value = [i.encode('utf-8') for i in value]
            elif isinstance(value, list):
                value = ",".join(value).encode('utf-8')
            options_list.append(
                '--{key}={value}'.format(key=key, value=repr(value)))
        return ' '.join(options_list)

    def prepare_playbook_args(ctx):
        playbook_source_path = \
            ctx.instance.runtime_properties.get('playbook_source_path', None)
        playbook_path = \
            ctx.instance.runtime_properties.get('playbook_path', None) \
            or ctx.instance.runtime_properties.get('site_yaml_path', None)
        sources = \
            ctx.instance.runtime_properties.get('sources', {})
        debug_level = \
            ctx.instance.runtime_properties.get('debug_level', 2)
        additional_args = \
            ctx.instance.runtime_properties.get('additional_args', '')
        additional_playbook_files = \
            ctx.instance.runtime_properties.get(
                'additional_playbook_files', None) or []
        ansible_env_vars = \
            ctx.instance.runtime_properties.get('ansible_env_vars', None) \
                or {'ANSIBLE_HOST_KEY_CHECKING': "False"}
        ctx.instance.runtime_properties[WORKSPACE] = tempfile.mkdtemp()
        # check if source path is provided [full path/URL]
        if playbook_source_path:
            # here we will combine playbook_source_path with playbook_path
            playbook_tmp_path = get_shared_resource(playbook_source_path)
            if playbook_tmp_path == playbook_source_path:
                # didn't download anything so check the provided path
                # if file and absolute path
                if os.path.isfile(playbook_tmp_path) and \
                        os.path.isabs(playbook_tmp_path):
                    # check file type if archived
                    file_name = playbook_tmp_path.rsplit('/', 1)[1]
                    file_type = file_name.rsplit('.', 1)[1]
                    if file_type == 'zip':
                        playbook_tmp_path = \
                            unzip_archive(playbook_tmp_path)
                    elif file_type in TAR_FILE_EXTENSTIONS:
                        playbook_tmp_path = \
                            untar_archive(playbook_tmp_path)
            playbook_path = "{0}/{1}".format(playbook_tmp_path,
                                             playbook_path)
        else:
            # here will handle the bundled ansible files
            playbook_path = handle_site_yaml(
                playbook_path, additional_playbook_files, ctx)
        playbook_args = {
            'playbook_path': playbook_path,
            'sources': handle_sources(sources, playbook_path, ctx),
            'verbosity': debug_level,
            'additional_args': additional_args or '',
        }
        options_config = \
            ctx.instance.runtime_properties.get('options_config', {})
        run_data = \
            ctx.instance.runtime_properties.get('run_data', {})
        return playbook_args, ansible_env_vars, options_config, run_data

    playbook_args, ansible_env_vars, options_config, run_data = \
        prepare_playbook_args(ctx)
    docker_ip = \
        ctx.node.properties.get('docker_machine',{}).get('docker_ip',"")
    docker_user = \
        ctx.node.properties.get('docker_machine',{}).get('docker_user',"")
    docker_key = \
        ctx.node.properties.get('docker_machine',{}).get('docker_key',"")
    container_volume = \
        ctx.node.properties.get('docker_machine',{}).get('container_volume',"")
    # The decorators will take care of creating the playbook workspace
    # which will package everything in a directory for our usages
    # it will be in the kwargs [playbook_args.playbook_path]
    playbook_path = playbook_args.get("playbook_path", "")
    debug_level = playbook_args.get("debug_level", 2)
    destination = os.path.dirname(os.path.dirname(playbook_path))
    verbosity = '-v'
    for i in range(1, debug_level):
        verbosity += 'v'
    command_options = \
        prepare_options_config(options_config, run_data, destination)
    additional_args = playbook_args.get("additional_args", "")
    if not destination:
        ctx.logger.error("something is wrong with the playbook provided")
        return
    else:
        ctx.logger.info("playbook is ready at {0}".format(destination))
        playbook_path = playbook_path.replace(destination, container_volume)
        command_options = command_options.replace(destination, container_volume)
        ctx.instance.runtime_properties['destination'] = destination
        ctx.instance.runtime_properties['docker_host'] = docker_ip
        ctx.instance.runtime_properties['ansible_env_vars'] = ansible_env_vars
        ctx.instance.runtime_properties['ansible_container_command_arg'] = \
            "ansible-playbook {0} -i hosts {1} {2} {3} ".format(
                verbosity,
                command_options,
                additional_args,
                playbook_path)
    # copy these files to docker machine if needed at that destination
    if not docker_ip:
        ctx.logger.info("no docker_ip was provided")
        return
    if docker_ip not in (LOCAL_HOST_ADDRESSES, get_lan_ip()):
        with get_fabric_settings(docker_ip,
                                 docker_user,
                                 docker_key):
            destination_parent = destination.rsplit('/', 1)[0]
            if destination_parent != '/tmp':
                sudo('mkdir -p {0}'.format(destination_parent))
                sudo("chown -R {0}:{0} {1}".format(docker_user,
                                                   destination_parent))
            put(destination, destination_parent, mirror_local_mode=True)



@operation
def remove_ansible_playbook(ctx, **kwargs):

    docker_ip = \
        ctx.node.properties.get('docker_machine',{}).get('docker_ip',"")
    docker_user = \
        ctx.node.properties.get('docker_machine',{}).get('docker_user',"")
    docker_key = \
        ctx.node.properties.get('docker_machine',{}).get('docker_key',"")

    destination = ctx.instance.runtime_properties.get('destination',"")
    if not destination:
        ctx.logger.error("destination was not assigned due to error")
        return
    ctx.logger.info("removing file from destination {0}".format(destination))
    shutil.rmtree(destination)
    ctx.instance.runtime_properties.pop('destination', None)
    if not docker_ip:
        ctx.logger.info("no docker_ip was provided")
        return
    if docker_ip not in (LOCAL_HOST_ADDRESSES, get_lan_ip()):
        with get_fabric_settings(docker_ip, docker_user, docker_key):
            sudo("rm -rf {0}".format(destination))
