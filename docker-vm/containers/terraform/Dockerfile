FROM amd64/centos:7

MAINTAINER Cosmo (hello@cloudify.co)

RUN yum install -y unzip

RUN curl https://releases.hashicorp.com/terraform/0.11.7/terraform_0.11.7_linux_amd64.zip -o terraform_0.11.7_linux_amd64.zip

RUN unzip terraform_0.11.7_linux_amd64.zip

RUN chmod a+x terraform

RUN mv terraform /usr/bin

ENTRYPOINT ["terraform"]
