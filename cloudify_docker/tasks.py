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

from cloudify.decorators import operation
from cloudify.exceptions import NonRecoverableError

from cloudify_common_sdk.resource_downloader import unzip_archive
from cloudify_common_sdk.resource_downloader import untar_archive
from cloudify_common_sdk.resource_downloader import get_shared_resource
from cloudify_common_sdk.resource_downloader import TAR_FILE_EXTENSTIONS

HOSTS_FILE_NAME = 'hosts'
CONTAINER_VOLUME = "container_volume"
PLAYBOOK_PATH = "playbook_path"
ANSIBLE_PRIVATE_KEY = 'ansible_ssh_private_key_file'
LOCAL_HOST_ADDRESSES = ("127.0.0.1", "localhost")


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
        ctx.node.properties.get('resource_config',{}).get('docker_ip',"")
    docker_user = \
        ctx.node.properties.get('resource_config',{}).get('docker_user',"")
    docker_key = \
        ctx.node.properties.get('resource_config',{}).get('docker_key',"")
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
