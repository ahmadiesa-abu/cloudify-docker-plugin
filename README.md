# Cloudify Docker Plugin

This plugin can be used to connect to docker machine and execute :
  * List docker machine images
  * List docker machine containers
  * List docker machine details
  * Build/Remove images from a Dockerfile [ as content ]
  * Create/Remove containers given the built images
    tags installed on the docker machine

if you don't have a working docker machine ready for integration
, you can build the docker machine using Cloudify manager if needed
using the blueprints attached in this repository:

See [Blueprints](docker-vm)

## Usage

See [Docker Plugin](https://docs.cloudify.co/5.0.5/working_with/official_plugins/)
