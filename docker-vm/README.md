# Docker VM Setup

This repository contains several Blueprints that will help you create docker machine on different Cloud Platforms.

If you're only now starting to work with Cloudify see our [Getting Started Guide](https://cloudify.co/getting-started/).

This document will guide you how to run the examples step by step.


## Using the CLI


### Prepering the environment

Download the archive.
```shell
curl -L https://codeload.github.com/ahmadiesa-abu/cloudify-docker-plugin/zip/master -o cloudify-docker-plugin.zip
```

Extract the archive.
```shell
unzip cloudify-docker-plugin.zip && cd cloudify-docker-plugin-master/docker-vm
```

Install Cloudify plugins that you may need for the required Cloud Platform.
```shell
cfy plugin upload -y {URL_TO_YAML} {URL_TO_WAGON}
```


### Creating secrets

Create secrets according to your IaaS provider.

Replace <value> with actual values, without the <>

For **AWS**
```shell
cfy secrets create aws_access_key_id --secret-string <value>
cfy secrets create aws_secret_access_key --secret-string <value>
```

For **Azure**
```shell
cfy secrets create azure_subscription_id --secret-string <value>
cfy secrets create azure_tenant_id --secret-string <value>
cfy secrets create azure_client_id --secret-string <value>
cfy secrets create azure_client_secret --secret-string <value>
```

For **GCP**

gcp_credentials: A GCP service account key in JSON format. **Hint: Create this secret from a file:**
```shell   
`cfy secrets create gcp_credentials -f ./path/to/JSON key`.
```                                             
gcp_zone: A GCP Zone such as `us-east1-b`:                                                              

```shell
cfy secrets create gcp_zone --secret-string <zone>                                                                                                                                              
```

For **Openstack**
```shell
cfy secrets create openstack_username --secret-string <value>
cfy secrets create openstack_password --secret-string <value>
cfy secrets create openstack_tenant_name --secret-string <value>
cfy secrets create openstack_auth_url --secret-string <value>
```


### Running the example


For **AWS**:

```shell
cfy install aws.yaml -i aws_region_name=<aws_region_name>
```

For **Azure**:

```shell
cfy install azure.yaml -i location=<location> -i agent_password=<agent_password>
```

For **GCP**:

```shell
cfy install gcp.yaml -i region=<region>
```

For **Openstack**:

```shell
cfy install openstack.yaml \
    -i region=RegionOne
    -i external_network=<external_network_name> \
    -i image=<you_linux_image_id> \
    -i flavor=<your_chosen_flavor_id>
```


###Get deployment id:       
```shell
cfy deployments list         
```
###Get the URL of the webserver
```shell
cfy deployment outputs <deployment_id>
```
## Using the Web UI

### Open the Web UI

1. Open the browser with the Cloudify Manager's public IP provided during installation
2. Login with user 'admin', password can be either:
    * Using one of the images (AMI, QCOW, Docker), password is 'admin'
    * Password provided during installation (in config.yaml)
    * Password generated during installation and printed to screen

### Install Cloudify plugins

1. Go to 'Cloudify Catalog' on the left side menu
2. In the 'Plugins Catalog' widget, select the plugin of the IaaS of your choice and click 'install' button on the right side

### Creating secrets

1. Go to 'System Resources on the left side menu'
2. Scroll down to the 'Secret Store Management' widget
3. Create secrets using the 'Create' button and according to your IaaS provider:
('Secret key' according to the list below and 'Secret value' with your specific values)
    * For **AWS**
        * aws_access_key_id
        * aws_secret_access_key
    * For **Azure**
        * subscription_id
        * tenant_id
        * client_id
        * client_secret
    * For **GCP**
        * gcp_client_x509_cert_url
        * gcp_client_email
        * gcp_client_id
        * gcp_project_id
        * gcp_private_key_id
        * gcp_private_key
        * gcp_project_id
        * gcp_zone
    * For **Openstack**
         * keystone_username
         * keystone_password
         * keystone_tenant_name
         * keystone_url


### Running the example

1. Go to 'Local Blueprints' menu and click 'Upload' button
2. In the blueprint choose an archived version of docker-vm folder
3. In the blueprint YAML file select one of the following  
    * azure.yaml
    * openstack.yaml
    * aws.yaml
    * gcp.yaml
4. Click 'upload'
5. In the blueprints table, click the 'cloudify-hello-world-example-master' link
6. Click 'Create Deployment' button
7. Type 'cloudify-hello-world-example-master' in the deployment name field
8. Complete the inputs' values:
    * For **AWS**
        * aws_region_name, for example 'eu-central-1'
    * For **Azure**
        * location, for example 'eastus'
        * agent_password, for example 'OpenS3sVm3'
    * For **GCP**
        * region, for example 'europe-west1'
    * For **Openstack**
         * region, for example 'RegionOne'
         * external_network, for example 'GATEWAY_NET'
         * image, for example '05bb3a46-ca32-4032-bedd-8d7ebd5c8100'
         * flavor, for example '4d798e17-3439-42e1-ad22-fb956ec22b54'
9. Click 'Deploy'
10. Scroll down to the deployments table, click the hanburger menu on the right and select 'Install'
