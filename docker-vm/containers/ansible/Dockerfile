FROM amd64/centos:7

MAINTAINER Cosmo (hello@cloudify.co)

RUN curl "https://bootstrap.pypa.io/get-pip.py" -o "get-pip.py"

RUN python get-pip.py

RUN pip install --upgrade pip==9.0.1 && pip install ansible==2.9.5

ENV ANSIBLE_HOST_KEY_CHECKING False
ENV ANSIBLE_INVALID_TASK_ATTRIBUTE_FAILED False

ENTRYPOINT ["ansible-playbook"]
